"""Opt-in OS constraints for offline verification and policy workers."""

import os
import resource
import socket

MIB = 1024**2


def cap_address_space(max_bytes):
    inherited = resource.getrlimit(resource.RLIMIT_AS)
    effective = min([max_bytes, *(limit for limit in inherited if limit != resource.RLIM_INFINITY)])
    resource.setrlimit(resource.RLIMIT_AS, (effective, effective))
    return effective


def apply_limits(*, cpus=None, process_memory_mib=None, offline=False):
    if cpus is not None and cpus < 1:
        raise ValueError("CPU count must be positive")
    if process_memory_mib is not None and process_memory_mib < 1:
        raise ValueError("Process memory limit must be positive")

    observations = {}
    if offline:
        interfaces = {name for _, name in socket.if_nameindex()}
        if interfaces != {"lo"}:
            raise ValueError("Offline verification requires a loopback-only network namespace")
        observations["loopback_only"] = True
    if cpus is not None:
        current = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, current[:cpus])
        observations["cpu_affinity"] = sorted(os.sched_getaffinity(0))
    if process_memory_mib is not None:
        observations["process_address_space_bytes"] = cap_address_space(process_memory_mib * MIB)
    return observations
