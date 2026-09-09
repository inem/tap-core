"""Bounded host filesystem sensor primitives.

The sensor reports what ``lstat`` returned or raised.  It does not assign TAP
health, validity, freshness, or ownership meanings to the addressed object.
"""
import errno
import os
import stat


def inspect_path(path):
    """Return one uniform receipt for one path without following symlinks."""
    try:
        status = os.lstat(path)
    except OSError as error:
        outcome = "absent" if error.errno in (errno.ENOENT, errno.ENOTDIR) else "failed"
        return {
            "outcome": outcome,
            "kind": None,
            "errno": error.errno,
            "message": str(error),
        }
    if stat.S_ISREG(status.st_mode):
        kind = "regular"
    elif stat.S_ISLNK(status.st_mode):
        kind = "symlink"
    else:
        kind = "other"
    return {"outcome": "present", "kind": kind, "errno": None, "message": ""}


def inspect_sized_path(path):
    """Return one lstat receipt with a byte size for regular files only."""
    try:
        status = os.lstat(path)
    except OSError as error:
        outcome = "absent" if error.errno in (errno.ENOENT, errno.ENOTDIR) else "failed"
        return {
            "outcome": outcome, "kind": None, "size_bytes": None,
            "errno": error.errno, "message": str(error),
        }
    if stat.S_ISREG(status.st_mode):
        kind, size_bytes = "regular", status.st_size
    elif stat.S_ISLNK(status.st_mode):
        kind, size_bytes = "symlink", None
    else:
        kind, size_bytes = "other", None
    return {"outcome": "present", "kind": kind, "size_bytes": size_bytes,
            "errno": None, "message": ""}
