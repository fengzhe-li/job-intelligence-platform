"""One reentrant cross-thread/process lock for application and submission writes.

Uses flock on the supported local Unix runtime. Never hold this across executor I/O.
"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import fcntl
import os
import threading

_mutex = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


@contextmanager
def store_lock(root):
    root = Path(root).resolve()
    key = str(root)
    with _mutex:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, "held", {})
        _local.held = held
        if key in held:
            yield
            return
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(root / ".application-submission.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[key] = fd
            yield
        finally:
            held.pop(key, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with store_lock(self.root):
            return method(self, *args, **kwargs)
    return wrapper


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
