"""Linux syscall allowlist for the built-in, single-threaded policy worker."""

import ctypes
import errno
import platform
import resource

from .limits import MIB, cap_address_space

# Only operations needed by an already initialized interpreter and its JSON pipes.
WORKER_SYSCALLS = (
    "read", "write", "close", "exit", "exit_group",
    "brk", "mmap", "munmap", "mremap", "mprotect", "madvise",
    "futex", "clock_gettime", "rt_sigreturn", "rt_sigprocmask",
)


def enter_worker_sandbox():
    if platform.system() != "Linux":
        raise RuntimeError("Policy isolation requires Linux with libseccomp.so.2")

    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None

    allow = 0x7FFF0000
    deny = 0x00050000 | errno.EPERM
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cap_address_space(256 * MIB)
    context = library.seccomp_init(deny)
    if not context:
        raise RuntimeError("Could not initialize policy isolation")
    try:
        for syscall in WORKER_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(syscall.encode("ascii"))
            if number < 0 or library.seccomp_rule_add(context, allow, number, 0) != 0:
                raise RuntimeError(f"Could not allow required syscall: {syscall}")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("Could not activate policy isolation")
    finally:
        library.seccomp_release(context)
