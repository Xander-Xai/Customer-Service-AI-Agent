"""分布式并发原语（per-thread 锁等）。"""

from .distributed_lock import (
    ThreadBusyError,
    ThreadLockUnavailableError,
    get_api_lock_manager,
    reset_api_lock_manager_for_tests,
    shutdown_api_lock_manager,
    thread_busy_payload,
    thread_lock,
)

__all__ = [
    "ThreadBusyError",
    "ThreadLockUnavailableError",
    "get_api_lock_manager",
    "reset_api_lock_manager_for_tests",
    "shutdown_api_lock_manager",
    "thread_busy_payload",
    "thread_lock",
]
