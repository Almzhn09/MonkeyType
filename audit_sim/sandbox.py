"""Isolation for the built-in, single-threaded policy worker.

Linux: a seccomp syscall allowlist enforced by the kernel (requires libseccomp.so.2).
macOS and Windows: an interpreter audit hook that denies file, network, process and
native-code access. The hook cannot be removed once installed, but unlike seccomp it does
not constrain native extension code, so it is weaker than the Linux kernel filter.
"""

import errno
import sys

# Only operations needed by an already initialized interpreter and its JSON pipes.
WORKER_SYSCALLS = (
    "read", "write", "close", "exit", "exit_group",
    "brk", "mmap", "munmap", "mremap", "mprotect", "madvise",
    "futex", "clock_gettime", "rt_sigreturn", "rt_sigprocmask",
)

MEMORY_LIMIT = 256 * 1024**2

# Audit events (https://docs.python.org/3/library/audit_events.html) a policy never needs.
BLOCKED_EVENTS = frozenset({"open", "builtins.breakpoint", "sys.remote_exec"})
BLOCKED_PREFIXES = (
    "os.", "socket.", "subprocess.", "shutil.", "ctypes.", "_ctypes.", "mmap.", "glob.",
    "tempfile.", "sqlite3.", "urllib.", "http.", "ftplib.", "smtplib.", "poplib.", "imaplib.",
    "nntplib.", "telnetlib.", "webbrowser.", "pty.", "fcntl.", "resource.", "syslog.",
    "_posixsubprocess.", "_winapi.", "winreg.", "msvcrt.", "_wmi.",
)


def isolation_kind():
    return "seccomp" if sys.platform.startswith("linux") else "audit-hook"


def enter_worker_sandbox():
    if isolation_kind() == "seccomp":
        _enter_seccomp()
    else:
        _enter_audit_hook()


def _set_rlimits():
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))


def _enter_seccomp():
    import ctypes

    try:
        library = ctypes.CDLL("libseccomp.so.2")
    except OSError as error:
        raise RuntimeError("Policy isolation on Linux requires libseccomp.so.2") from error
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None

    allow = 0x7FFF0000
    deny = 0x00050000 | errno.EPERM
    _set_rlimits()
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


def _enter_audit_hook():
    if sys.platform != "win32":
        try:
            _set_rlimits()
        except (ImportError, ValueError, OSError):
            # macOS often rejects RLIMIT_AS; the runner's deadline still bounds the worker.
            pass

    blocked_events, blocked_prefixes = BLOCKED_EVENTS, BLOCKED_PREFIXES

    def deny(event, _args):
        if event in blocked_events or event.startswith(blocked_prefixes):
            raise PermissionError(errno.EPERM, f"Operation not permitted in policy worker: {event}")

    sys.addaudithook(deny)
