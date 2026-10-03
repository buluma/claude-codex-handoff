#!/usr/bin/env python3
"""Archive the fully-consumed prefix of a handoff stream (PROTOCOL.md §13).

Append-only streams grow without bound. This tool moves the prefix that *every*
reading-side session has already consumed into `.handoff-runtime/archive/`, then
atomically rewrites the live stream with only the still-unconsumed tail. It never
drops a line any reader hasn't passed, and never drops the latest line.

Stdlib only, so it runs from PowerShell, bash, Claude, or Codex without setup.

Usage:
    python .handoff/tools/archive.py            # archive both streams
    python .handoff/tools/archive.py --dry-run  # report only, write nothing
    python .handoff/tools/archive.py --stream c2x
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402  (import follows the path bootstrap above)
    HandoffError,
    SendLock,
    STREAMS,
    atomic_write_text,
    find_project_root,
    iter_jsonl_lines,
    parse_msg_seq,
    read_int,
    runtime_dir,
)


def find_runtime(start: Path) -> Path:
    return runtime_dir(find_project_root(start))


def reader_archive_point(runtime: Path, reader_side: str) -> int | None:
    """Minimum consumed seq across every reader-side session cursor.

    None when no reader cursor exists at all, in which case archiving anything
    could drop an unconsumed line, so we skip (PROTOCOL.md §13).
    """
    cursors_dir = runtime / "cursors"
    if cursors_dir.is_dir():
        values = []
        for path in cursors_dir.iterdir():
            if path.is_file() and path.name.startswith(reader_side + "-"):
                value = read_int(path)
                if value is not None:
                    values.append(value)
        if values:
            return min(values)
    # No per-session cursor yet (pre-v1.9 runtime): fall back to the legacy
    # shared anchor. Once per-session cursors exist they are the source of truth.
    return read_int(runtime / f".{reader_side}-cursor")


def _seq_of(line: str) -> int | None:
    """Seq of a JSONL line, or None when the line is unusable."""
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None
    return parse_msg_seq(msg.get("id"))


def partition(
    stream: Path, point: int
) -> tuple[list[str], list[str], list[int], int | None]:
    """Split a stream into (to_archive, retained, bad lines, newest seq).

    A line is archived only when its seq is known, at or below `point`, and is
    not the newest line in the stream. A line with no parseable id has no seq,
    so it is always retained rather than guessed at.
    """
    entries: list[tuple[int | None, str]] = []
    bad: list[int] = []
    for line_no, line in iter_jsonl_lines(stream):
        seq = _seq_of(line)
        if seq is None:
            bad.append(line_no)
        entries.append((seq, line))

    known = [seq for seq, _ in entries if seq is not None]
    newest = max(known) if known else None

    to_archive: list[str] = []
    retained: list[str] = []
    for seq, line in entries:
        if seq is not None and seq <= point and (newest is None or seq < newest):
            to_archive.append(line)
        else:
            retained.append(line)
    return to_archive, retained, bad, newest


def archive_stream(runtime: Path, key: str, dry_run: bool) -> str:
    filename, writer_side, reader_side = STREAMS[key]
    stream = runtime / filename
    if not stream.is_file():
        return f"{key}: stream missing, skip"

    point = reader_archive_point(runtime, reader_side)
    if point is None:
        return f"{key}: no {reader_side} cursor yet, skip (nothing safe to archive)"
    if point <= 0:
        return f"{key}: reader at seq {point}, nothing consumed, skip"

    with SendLock(runtime, writer_side):
        to_archive, retained, bad, newest = partition(stream, point)
        if bad:
            # A line we cannot read has no seq, so we cannot prove any reader
            # consumed it. Refuse rather than risk dropping it.
            return (
                f"{key}: {len(bad)} line(s) have no parseable id "
                f"(first at line {bad[0]}); refusing to archive, run doctor.py"
            )
        if not to_archive:
            return (
                f"{key}: archive_point={point}, nothing below it "
                f"(newest seq {newest}); skip"
            )
        if dry_run:
            return (
                f"{key}: would archive {len(to_archive)} line(s) (seq<= {point}), "
                f"keep {len(retained)} (DRY-RUN)"
            )

        ts = _timestamp()
        archive_dir = runtime / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{filename}.{ts}.jsonl"
        suffix = 1
        while archive_path.exists():
            archive_path = archive_dir / f"{filename}.{ts}.{suffix:03d}.jsonl"
            suffix += 1
        atomic_write_text(archive_path, "\n".join(to_archive) + "\n")
        atomic_write_text(stream, ("\n".join(retained) + "\n") if retained else "")
        return (
            f"{key}: archived {len(to_archive)} line(s) -> "
            f"archive/{archive_path.name}, kept {len(retained)}"
        )


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive consumed handoff stream prefix (PROTOCOL.md §13)."
    )
    parser.add_argument(
        "--stream", choices=sorted(STREAMS), help="Only this stream; default both."
    )
    parser.add_argument(
        "--root", type=Path, help="Project root; defaults to auto-detected."
    )
    parser.add_argument("--dry-run", "--verify", dest="dry_run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        runtime = find_runtime(args.root.resolve() if args.root else Path.cwd())
    except HandoffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    keys = [args.stream] if args.stream else list(STREAMS)
    for key in keys:
        try:
            print(archive_stream(runtime, key, args.dry_run))
        except HandoffError as exc:
            print(f"error: {key}: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
