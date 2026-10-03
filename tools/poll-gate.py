#!/usr/bin/env python3
"""Deterministic pre-gate for a claude-codex-handoff polling pass.

Answers one question without any model call: should this cron / heartbeat fire
actually wake the model, or is it a pure-idle tick that a program can finish on
its own?

It resolves the same project root, per-session cursor, and session id as
`send.py` / `doctor.py`, reads the inbound stream, and classifies the unread
range (seq > cursor) by addressee and lease vs pure-consumption.

Usage (run before invoking the model in cron-prompt.md / codex-heartbeat):

    python .handoff/tools/poll-gate.py --side claude
    python .handoff/tools/poll-gate.py --side codex --session codex-main --proactive-every 3

Default mode is READ-ONLY and stateless: it decides only `process` vs `idle`.
`--proactive-every N` adds the `proactive` decision: after N consecutive idle
ticks for this session it signals one bounded proactive-review tick, persisting
a small idle-streak counter in `.handoff-runtime/.<side>-pollgate.json`.

Exit codes (so a shell wrapper can branch without parsing stdout):

      0  process   -- unread messages addressed to me exist: WAKE the model
     10  proactive -- no unread for me, but a proactive-review tick is due: WAKE
     20  idle      -- nothing for me and proactive not due: skip the model
      2  error     -- no runtime / bad arguments

stdout is a one-line JSON summary unless --quiet is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402  (import follows the path bootstrap above)
    HandoffError,
    INBOUND,
    atomic_write_text,
    find_project_root,
    iter_jsonl_lines,
    now_iso,
    parse_msg_seq,
    resolve_cursor,
    resolve_session_id,
)

# PROTOCOL.md 4: lease-bearing vs pure-consumption types.
LEASE_TYPES = {"task", "handoff", "question", "cancel", "error"}
PURE_TYPES = {"status", "done"}

EXIT_PROCESS = 0
EXIT_PROACTIVE = 10
EXIT_IDLE = 20
EXIT_ERROR = 2


def read_inbound(runtime: Path, side: str) -> tuple[int, list[dict[str, Any]]]:
    """Parse the inbound stream, dropping lines with no usable id."""
    path = runtime / INBOUND[side][0]
    max_seq = 0
    messages: list[dict[str, Any]] = []
    for _line_no, line in iter_jsonl_lines(path):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        seq = parse_msg_seq(msg.get("id"))
        if seq is None:
            continue
        max_seq = max(max_seq, seq)
        messages.append(msg)
    return max_seq, messages


def classify(
    messages: list[dict[str, Any]], cursor: int, session: str
) -> dict[str, int]:
    """Bucket the unread range (seq > cursor) by who it is for and what kind."""
    counts = {
        "unread_total": 0,
        "for_me_lease": 0,
        "for_me_pure": 0,
        "directed_elsewhere": 0,
        "unknown_type": 0,
    }
    for msg in messages:
        seq = parse_msg_seq(msg.get("id"))
        if seq is None or seq <= cursor:
            continue
        counts["unread_total"] += 1
        to_session = msg.get("to_session")
        if isinstance(to_session, str) and to_session and to_session != session:
            counts["directed_elsewhere"] += 1
            continue
        msg_type = msg.get("type")
        if msg_type in LEASE_TYPES:
            counts["for_me_lease"] += 1
        elif msg_type in PURE_TYPES:
            counts["for_me_pure"] += 1
        else:
            counts["unknown_type"] += 1
    return counts


def proactive_state_path(runtime: Path, side: str) -> Path:
    return runtime / f".{side}-pollgate.json"


def load_proactive_streak(runtime: Path, side: str, session: str) -> int:
    path = proactive_state_path(runtime, side)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return 0
    entry = sessions.get(session)
    if isinstance(entry, dict) and isinstance(entry.get("idle_streak"), int):
        return max(0, entry["idle_streak"])
    return 0


def save_proactive_streak(
    runtime: Path, side: str, session: str, streak: int, decision: str
) -> None:
    path = proactive_state_path(runtime, side)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    sessions[session] = {
        "idle_streak": streak,
        "last_decision": decision,
        "updated_at": now_iso(),
    }
    data["sessions"] = sessions
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def decide(
    counts: dict[str, int],
    runtime: Path,
    side: str,
    session: str,
    proactive_every: int,
) -> tuple[str, int]:
    """Pick process / proactive / idle.

    An `unknown_type` message is treated as work rather than ignored: a type
    this gate does not know about may still be a real request, and skipping it
    would drop it silently. It is surfaced separately in the summary so the
    model can see why it was woken.
    """
    for_me = counts["for_me_lease"] + counts["for_me_pure"] + counts["unknown_type"]
    if for_me > 0:
        if proactive_every > 0:
            save_proactive_streak(runtime, side, session, 0, "process")
        return "process", EXIT_PROCESS

    if proactive_every <= 0:
        return "idle", EXIT_IDLE

    streak = load_proactive_streak(runtime, side, session) + 1
    if streak >= proactive_every:
        save_proactive_streak(runtime, side, session, 0, "proactive")
        return "proactive", EXIT_PROACTIVE
    save_proactive_streak(runtime, side, session, streak, "idle")
    return "idle", EXIT_IDLE


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic wake/idle gate for a handoff polling pass.",
        epilog="exit codes: 0 process, 10 proactive, 20 idle, 2 error",
    )
    parser.add_argument("--side", required=True, choices=sorted(INBOUND))
    parser.add_argument(
        "--session",
        help="Session id; defaults like send.py (env / .<side>-session / <side>-default).",
    )
    parser.add_argument(
        "--root", type=Path, help="Project root; defaults to auto-detected."
    )
    parser.add_argument(
        "--proactive-every",
        type=int,
        default=0,
        metavar="N",
        help="Signal one proactive-review tick after N consecutive idle ticks (0 = disabled, read-only).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the JSON summary; rely on exit code only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    def emit(summary: dict[str, Any]) -> None:
        if not args.quiet:
            print(json.dumps(summary, ensure_ascii=False))

    root = args.root.resolve() if args.root else find_project_root(Path.cwd())
    runtime = root / ".handoff-runtime"
    if not runtime.is_dir():
        emit({"decision": "error", "reason": "no .handoff-runtime"})
        return EXIT_ERROR

    try:
        # Same resolver as send.py and doctor.py, so the gate can never gate on
        # a different cursor than the one the session actually maintains.
        session = resolve_session_id(args.side, args.session, runtime)
    except HandoffError as exc:
        emit({"decision": "error", "side": args.side, "reason": str(exc)})
        return EXIT_ERROR

    cursor = resolve_cursor(runtime, args.side, session)
    stream_max, messages = read_inbound(runtime, args.side)
    counts = classify(messages, cursor.value, session)
    decision, code = decide(counts, runtime, args.side, session, args.proactive_every)

    for_me = counts["for_me_lease"] + counts["for_me_pure"] + counts["unknown_type"]
    summary = {
        "side": args.side,
        "session": session,
        "cursor": cursor.value,
        "cursor_source": cursor.source,
        "cursor_file": cursor.used_filename or cursor.filename,
        "stream_max_seq": stream_max,
        "decision": decision,
        "unread_total": counts["unread_total"],
        "for_me": for_me,
        "for_me_lease": counts["for_me_lease"],
        "for_me_pure": counts["for_me_pure"],
        "directed_elsewhere": counts["directed_elsewhere"],
        "unknown_type": counts["unknown_type"],
    }
    emit(summary)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except HandoffError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
