"""Bounded read snapshots and opaque positions for the existing rotating JSONL.

Internal storage API for #9; no scheduler, automatic acknowledgements or replay.
"""
import base64
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import stat as stat_mode
from pathlib import Path
import re

from .records import MAX_RECORD_BYTES, RecordError, decode_record


class JournalError(ValueError):
    pass


class JournalChanged(JournalError):
    """An unstable listing or file needs a new scan with the SAME saved cursor."""


class JournalGap(JournalError):
    """The saved position cannot be verified; explicit replay is required."""


@dataclass(frozen=True)
class Entry:
    record: dict
    cursor: str


def digest(value):
    return hashlib.sha256(value).hexdigest()


class Journal:
    def __init__(self, data, max_record_bytes=MAX_RECORD_BYTES):
        self.data = Path(data).resolve()
        if type(max_record_bytes) is not int or max_record_bytes < 1:
            raise ValueError("max_record_bytes must be a positive integer")
        self.max_record_bytes = max_record_bytes
        self.scope = digest(str(self.data).encode())

    def paths(self):
        # Read writer-generated numeric archives; unrelated files never enter replay.
        archives = []
        current = None
        try:
            with os.scandir(self.data) as entries:
                for entry in entries:
                    if entry.name == "stream.jsonl":
                        current = Path(entry.path)
                    elif re.fullmatch(r"stream\.jsonl\.[0-9]+", entry.name):
                        archives.append(Path(entry.path))
                    if len(archives) + (current is not None) > 64:
                        raise JournalError("Too many capture segments for one snapshot (maximum 64)")
        except OSError as error:
            raise JournalError("Cannot list capture directory") from error
        archives.sort(key=lambda path: int(path.suffix[1:]))
        return archives + ([current] if current is not None else [])

    @contextmanager
    def snapshot(self):
        # Pin files with descriptors, so a concurrent rename/unlink cannot turn
        # the selected old current file into the new current file mid-read.
        with ExitStack() as stack:
            paths = self.paths()
            selected = []
            try:
                for path in paths:
                    if path.is_symlink():
                        raise JournalError("Capture segment must not be a symlink")
                    if not stat_mode.S_ISREG(path.stat().st_mode):
                        raise JournalError("Capture segment must be a regular file")
                    handle = stack.enter_context(path.open("rb"))
                    stat = os.fstat(handle.fileno())
                    selected.append((path, handle, stat))
                if paths != self.paths():
                    raise JournalChanged("Capture rotated during listing; retry the saved cursor")
                identities = set()
                for path, handle, stat in selected:
                    current = path.stat()
                    identity = (stat.st_dev, stat.st_ino)
                    if identity != (current.st_dev, current.st_ino) or identity in identities:
                        raise JournalChanged("Capture segment replaced during listing; retry the saved cursor")
                    identities.add(identity)
            except FileNotFoundError as error:
                raise JournalChanged("Capture rotated during open; retry the saved cursor") from error
            except OSError as error:
                raise JournalError("Cannot open or inspect capture segments") from error
            yield selected

    def lines(self, path, handle, size):
        remaining = size
        while remaining:
            try:
                line = handle.readline(min(remaining, self.max_record_bytes + 1))
            except OSError as error:
                raise JournalError("Cannot read capture segment") from error
            if not line:
                raise JournalChanged("Capture truncated while reading; retry the saved cursor")
            if len(line) > self.max_record_bytes:
                raise RecordError("Capture record exceeds reader allocation limit")
            remaining -= len(line)
            if not line.endswith(b"\n"):
                if path.name == "stream.jsonl" and not remaining:
                    return  # never acknowledge an unfinished active tail
                raise RecordError("Unfinished line in an archived capture segment")
            yield line

    def position(self, token):
        if not isinstance(token, str) or len(token) > 2048:
            raise JournalError("Invalid capture cursor")
        try:
            value = json.loads(base64.b64decode(token, altchars=b'-_', validate=True))
            if (not isinstance(value, dict) or set(value) != {"v", "scope", "segment", "line", "anchor"}
                    or type(value["v"]) is not int or value["v"] != 1
                    or type(value["line"]) is not int or value["line"] < 1
                    or any(not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                           for key in ("scope", "segment", "anchor"))):
                raise ValueError()
        except (ValueError, UnicodeError) as error:
            raise JournalError("Invalid or unsupported capture cursor") from error
        if value["scope"] != self.scope:
            raise JournalGap("Cursor belongs to another capture directory")
        return value

    def cursor(self, segment, index, line):
        value = {"v": 1, "scope": self.scope, "segment": segment, "line": index, "anchor": digest(line)}
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()

    def scan(self, after=None):
        """Yield complete records after a verified cursor, or all retained records.

        Persist an Entry.cursor only AFTER successful consumption. Close this
        generator if stopping early, to release pinned descriptors promptly.
        """
        saved = self.position(after) if after is not None else None
        with self.snapshot() as selected:
            segments = []
            seen = set()
            # Identify every segment before yielding: identical legacy first
            # lines are ambiguous, rather than grounds for silently skipping one.
            for path, handle, stat in selected:
                lines = self.lines(path, handle, stat.st_size)
                first = next(lines, None)
                if first is None:
                    continue
                segment = digest(first)
                if segment in seen:
                    raise JournalGap("Ambiguous capture segments have identical first records")
                seen.add(segment)
                segments.append((segment, path, handle, stat.st_size))
                handle.seek(0)
            if saved and saved["segment"] not in seen:
                raise JournalGap("Saved capture segment is unavailable; retention or replacement may have removed it")
            found = saved is None
            for segment, path, handle, size in segments:
                if not found and segment != saved["segment"]:
                    continue
                anchored = found
                for index, line in enumerate(self.lines(path, handle, size), 1):
                    if index == 1 and digest(line) != segment:
                        raise JournalChanged("Capture first record changed during scan; retry the saved cursor")
                    if not anchored:
                        if index != saved["line"]:
                            continue
                        if digest(line) != saved["anchor"]:
                            raise JournalGap("Saved capture record changed; refusing to advance")
                        decode_record(line)
                        anchored = found = True
                        continue
                    record = decode_record(line)
                    yield Entry(record, self.cursor(segment, index, line))
                if not anchored:
                    raise JournalGap("Saved capture position was truncated; refusing to advance")
