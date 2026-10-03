#!/usr/bin/env python3
"""Shared primitives for the claude-codex-handoff tools.

Stdlib only, by design: these helpers must run from PowerShell, Git Bash,
Claude, or Codex with no install step. Every tool imports this module so that
protocol invariants -- stream names, id grammar, session-id grammar, cursor
resolution, the send lock, and atomic+fsync writes -- have exactly one
implementation. When two tools resolve an invariant separately they eventually
disagree, and a disagreement in cursor resolution silently drops work.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# --------------------------------------------------------------------------
# Protocol vocabulary (PROTOCOL.md sections 1-3, 4)
# --------------------------------------------------------------------------

VALID_SIDES = ("claude", "codex")

# stream key -> (filename, writer side, reader side)
STREAMS = {
    "c2x": ("claude-to-codex.jsonl", "claude", "codex"),
    "x2c": ("codex-to-claude.jsonl", "codex", "claude"),
}

# outbound stream each side writes -> filename
OUTBOUND = {
    "claude": "claude-to-codex.jsonl",
    "codex": "codex-to-claude.jsonl",
}

# inbound stream each side polls -> (filename, writer side)
INBOUND = {
    "claude": ("codex-to-claude.jsonl", "codex"),
    "codex": ("claude-to-codex.jsonl", "claude"),
}

RUNTIME_DIRNAME = ".handoff-runtime"
HANDOFF_DIRNAME = ".handoff"

# Ids are `<side>-<seq>`. send.py generates zero-padded to 6 digits, but a
# hand-written line is not required to be padded, so every reader accepts any
# digit run. Parsing permissively keeps doctor.py from rejecting a stream that
# send.py itself would happily append after.
ID_RE = re.compile(r"^(codex|claude)-(\d+)$")

# PROTOCOL.md 3.1: ASCII letters, digits, `_`, `-`, `.`, `:`; length 1-64.
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

LOCK_TIMEOUT_SECONDS = 30.0
LOCK_STALE_SECONDS = 600.0

# PROTOCOL.md 3: `summary` is at most 200 characters; 180 is the point at which
# send.py spills the full text into a note and truncates.
SUMMARY_MAX = 200
SUMMARY_SPILL = 180
SUMMARY_MIN_ROOM = 20


class HandoffError(Exception):
    """Any condition the caller should see as a clean, explained failure."""


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def now_iso() -> str:
    """UTC ISO8601 with millisecond precision, as used by PROTOCOL.md 3."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------
# Durable writes (PROTOCOL.md 0 rule 9, 5, 6.1)
# --------------------------------------------------------------------------


def fsync_dir(path: Path) -> None:
    """Flush a directory entry so a completed rename survives a crash.

    `os.replace` is atomic with respect to readers, but without an fsync of the
    containing directory the rename itself can still be lost on power failure.
    Windows cannot open a directory for this; skip it there.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via temp file + fsync + rename + dir fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
        fsync_dir(path.parent)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _open_flags(base: int) -> int:
    if hasattr(os, "O_BINARY"):
        return base | os.O_BINARY
    return base


def write_new_file(path: Path, text: str) -> None:
    """Create `path` with `text`, failing if it already exists.

    Returns False when the file was already there, so callers can implement the
    reuse-an-existing-note rule in PROTOCOL.md 6.3 instead of crashing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, _open_flags(os.O_CREAT | os.O_EXCL | os.O_WRONLY), 0o666)
    except FileExistsError:
        return False
    try:
        view = memoryview(text.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)
    return True


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """Append one JSON line durably. Existing lines are never rewritten."""
    payload = (
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    fd = os.open(path, _open_flags(os.O_APPEND | os.O_CREAT | os.O_WRONLY), 0o666)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def read_int(path: Path) -> int | None:
    """Read a small integer runtime file, or None if absent/unreadable."""
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    try:
        return int(text or "0")
    except ValueError:
        return None


def read_seq_file(path: Path) -> int:
    """Read a `.<side>-seq` file, failing loudly rather than guessing.

    A corrupt seq file means id assignment cannot continue safely, so this
    raises instead of silently restarting the sequence.
    """
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise HandoffError(f"cannot read {path.name}: {exc}") from exc
    if not text:
        return 0
    try:
        value = int(text)
    except ValueError as exc:
        raise HandoffError(
            f"{path.name} is not an integer ({text!r}); delete it to rebuild from the stream"
        ) from exc
    if value < 0:
        raise HandoffError(f"{path.name} must not be negative (got {value})")
    return value


def parse_msg_seq(value: Any) -> int | None:
    """Extract the seq from a message id, or None if it is not a valid id."""
    if not isinstance(value, str):
        return None
    match = ID_RE.match(value)
    return int(match.group(2)) if match else None


def iter_jsonl_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (1-based line number, line content) for every non-blank line.

    The line is returned without its trailing newline, so callers can rejoin
    records with "\\n" without interleaving blank lines.
    """
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.rstrip("\r\n")
            if line.strip():
                yield line_no, line


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL stream into dicts, skipping unparseable lines."""
    messages: list[dict[str, Any]] = []
    for _line_no, line in iter_jsonl_lines(path):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.append(value)
    return messages


def max_seq(path: Path) -> int:
    """Highest seq present in a stream, or 0 if there are no valid ids."""
    highest = 0
    for msg in read_jsonl(path):
        seq = parse_msg_seq(msg.get("id"))
        if seq is not None:
            highest = max(highest, seq)
    return highest


def side_paths(runtime: Path, side: str) -> tuple[Path, Path, Path]:
    """(seq file, own outbox, peer stream) for a side.

    Kept as an explicit table rather than derived from the stream keys: getting
    a side's direction backwards would append to the peer's stream and silently
    corrupt the protocol, so there is nothing clever to get wrong here.
    """
    if side not in OUTBOUND:
        raise HandoffError(f"invalid side: {side}")
    return (
        runtime / f".{side}-seq",
        runtime / OUTBOUND[side],
        runtime / INBOUND[side][0],
    )


# --------------------------------------------------------------------------
# Project root (PROTOCOL.md 1)
# --------------------------------------------------------------------------


def find_project_root(start: Path) -> Path:
    """Nearest ancestor holding `.handoff-runtime/` or `.handoff/`.

    Accepts any of the layouts the kit is installed in: a project root, the
    `.handoff/` directory itself, or any subdirectory of either. Falls back to
    `start` so read-only tools can report a clean "not set up" error instead of
    a traceback.
    """
    current = start.resolve()
    for path in [current, *current.parents]:
        if (path / RUNTIME_DIRNAME).is_dir():
            return path
        if (path / HANDOFF_DIRNAME / "PROTOCOL.md").is_file():
            return path
        if (path / HANDOFF_DIRNAME).is_dir():
            return path
    return current


def runtime_dir(root: Path) -> Path:
    runtime = root / RUNTIME_DIRNAME
    if not runtime.is_dir():
        raise HandoffError(
            f"could not find {RUNTIME_DIRNAME}/; run bash .handoff/setup.sh "
            "(Windows: powershell -ExecutionPolicy Bypass -File .handoff\\setup.ps1)"
        )
    return runtime


# --------------------------------------------------------------------------
# Session ids (PROTOCOL.md 3.1)
# --------------------------------------------------------------------------


def validate_session_id(value: Any, field: str) -> str | None:
    """Return an error message if `value` is not a legal session id."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return f"{field} must be a non-empty string"
    if not SESSION_RE.match(value):
        return f"{field} must be 1-64 ASCII chars: letters, digits, _, -, ., :"
    return None


def resolve_session_id(side: str, explicit: str | None, runtime: Path) -> str:
    """Resolve MY_SESSION for `side` using the PROTOCOL.md 3.1 priority order.

    An explicitly supplied value (flag, or an environment variable a human set)
    must be valid or this raises: silently substituting a different session
    would send `from_session`/`to_session` values the caller never asked for.
    """
    candidates: list[tuple[str, str | None]] = [
        ("--session", explicit),
        ("HANDOFF_SESSION_ID", os.environ.get("HANDOFF_SESSION_ID")),
        (f"{side.upper()}_SESSION_ID", os.environ.get(f"{side.upper()}_SESSION_ID")),
    ]
    session_file = runtime / f".{side}-session"
    if session_file.is_file():
        try:
            candidates.append(
                (
                    f".handoff-runtime/.{side}-session",
                    session_file.read_text(encoding="utf-8").strip(),
                )
            )
        except OSError:
            pass

    for source, candidate in candidates:
        if not candidate:
            continue
        error = validate_session_id(candidate, "session")
        if error:
            raise HandoffError(f"{error} (from {source})")
        return candidate
    return f"{side}-default"


# --------------------------------------------------------------------------
# Per-session cursors (PROTOCOL.md 6.1)
# --------------------------------------------------------------------------


def cursor_filename(side: str, session: str) -> str:
    """Canonical cursor filename for a session.

    PROTOCOL.md 6.1 names the file `cursors/<side>-<session>`, but session ids
    conventionally already carry the side prefix (`claude-main` per 3.1, and the
    `<side>-default` fallback). Prefixing unconditionally would ask for
    `cursors/claude-claude-main`. When the session id already starts with
    `<side>-` the id alone is the name, so `claude-main` and `<side>-<session>`
    never diverge.
    """
    if session.startswith(f"{side}-"):
        return session
    return f"{side}-{session}"


def cursor_path_candidates(side: str, session: str) -> list[str]:
    """Every filename this session's cursor may live under, canonical first.

    Both spellings are accepted so a runtime written under either convention
    keeps its progress; see cursor_filename for why both can exist.
    """
    names = [cursor_filename(side, session)]
    legacy_style = f"{side}-{session}"
    if legacy_style not in names:
        names.append(legacy_style)
    return names


@dataclass(frozen=True)
class CursorResolution:
    """Where a session's consumption progress came from."""

    value: int
    source: str  # "cursor" | "legacy" | "default"
    filename: str  # cursor filename to read from and write back to
    used_filename: str | None  # the file actually read, when source == "cursor"


def resolve_cursor(runtime: Path, side: str, session: str) -> CursorResolution:
    """Resolve a session's cursor, preferring its own file over the anchor.

    PROTOCOL.md 6.1: fall back to the legacy shared `.<side>-cursor` only when
    this session has no cursor file, so history is not consumed twice on
    upgrade. Every tool must call this so the gate, the diagnostics, and the
    archiver agree on what "my cursor" means.
    """
    names = cursor_path_candidates(side, session)
    cursors_dir = runtime / "cursors"
    for name in names:
        value = read_int(cursors_dir / name)
        if value is not None:
            return CursorResolution(value, "cursor", names[0], name)
    legacy = read_int(runtime / f".{side}-cursor")
    if legacy is not None:
        return CursorResolution(legacy, "legacy", names[0], None)
    return CursorResolution(0, "default", names[0], None)


def write_cursor(runtime: Path, side: str, session: str, value: int) -> Path:
    """Persist a session cursor durably; returns the path written."""
    name = resolve_cursor(runtime, side, session).filename
    path = runtime / "cursors" / name
    atomic_write_text(path, f"{int(value)}\n")
    return path


def side_cursor_files(runtime: Path, side: str) -> list[Path]:
    """Existing cursor files that look like they belong to `side`."""
    cursors_dir = runtime / "cursors"
    if not cursors_dir.is_dir():
        return []
    found = []
    for path in sorted(cursors_dir.iterdir()):
        if path.is_file() and path.name.startswith(f"{side}-"):
            found.append(path)
    return found


# --------------------------------------------------------------------------
# Send lock (PROTOCOL.md 5)
# --------------------------------------------------------------------------


class SendLock:
    """Per-side lock so concurrent sends cannot assign duplicate ids.

    Only the holder removes the file, so a stale-lock recovery by another
    process cannot be undone out from under it.
    """

    def __init__(self, runtime: Path, side: str) -> None:
        self.path = runtime / "locks" / f"{side}-send.lock"
        self.side = side
        self.acquired = False

    def __enter__(self) -> "SendLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(
                    self.path, _open_flags(os.O_CREAT | os.O_EXCL | os.O_WRONLY), 0o666
                )
                try:
                    payload = {
                        "side": self.side,
                        "pid": os.getpid(),
                        "created_at": now_iso(),
                    }
                    view = memoryview(
                        json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    )
                    while view:
                        view = view[os.write(fd, view) :]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                if self._remove_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise HandoffError(
                        f"timed out after {LOCK_TIMEOUT_SECONDS:g}s waiting for send lock: {self.path}"
                    )
                time.sleep(0.1)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _remove_if_stale(self) -> bool:
        """Drop a lock left behind by a dead process. True if it is gone now."""
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age < LOCK_STALE_SECONDS:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
