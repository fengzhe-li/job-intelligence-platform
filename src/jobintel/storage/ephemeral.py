"""Ephemeral runtime state that must never travel with application data.

Local-browser runtime (live Chrome profiles with sockets/lock links, session records) lives in its
own ignored tree, data/runtime/, outside the application store data/local. Profiles created by
earlier versions may still sit at data/local/submissions/local_browser/{profiles,sessions} until
they are released; every copy/backup of data/local skips those two directories.
"""
from __future__ import annotations

from pathlib import Path

EPHEMERAL_DIRS = frozenset({"profiles", "sessions"})


def ignore_ephemeral(directory, names):
    """shutil.copytree `ignore` callable: skip legacy local-browser runtime inside a store copy."""
    path = Path(directory)
    if path.name == "local_browser" and path.parent.name == "submissions":
        return {name for name in names if name in EPHEMERAL_DIRS}
    return set()
