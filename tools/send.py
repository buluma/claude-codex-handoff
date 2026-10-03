#!/usr/bin/env python3
"""Write one handoff JSONL message with protocol-safe defaults.

This helper intentionally uses only the Python standard library so it can be
called from macOS, Linux, Claude, or Codex without extra setup.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

# Put this file's directory first so `import _common` works no matter how the
# tool is invoked (script path, symlink, or copied elsewhere on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402  (import follows the path bootstrap above)
    HandoffError,
    SendLock,
    SUMMARY_MAX,
    SUMMARY_MIN_ROOM,
    SUMMARY_SPILL,
    append_jsonl,
    atomic_write_text,
    find_project_root,
    max_seq,
    now_iso,
    parse_msg_seq,
    read_jsonl,
    read_seq_file,
    resolve_session_id,
    runtime_dir,
    side_paths,
    validate_session_id,
    write_new_file,
)


VALID_TYPES = {"task", "handoff", "done", "status", "question", "error", "cancel"}
VALID_STATES = {"claimed", "progress", "blocked", "awaiting-input", "shutdown"}
TERMINAL_TYPES = {"done", "error", "cancel"}

DURATION_RE = re.compile(
    r"^P((\d+D)(T(\d+H(\d+M)?(\d+S)?|\d+M(\d+S)?|\d+S))?|"
    r"T(\d+H(\d+M)?(\d+S)?|\d+M(\d+S)?|\d+S))$"
)

DUPLICATE_SCAN_WINDOW = 50


def utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def next_local_seq(seq_path: Path, outbox: Path) -> int:
    """PROTOCOL.md 5: max(.<side>-seq, real outbox max seq) + 1.

    Consulting the outbox as well recovers from a seq file that was reset or
    lost, so a restored-from-backup stream cannot reissue an existing id.
    """
    return max(read_seq_file(seq_path), max_seq(outbox)) + 1


def infer_thread(
    reply_to: str | None, explicit: str | None, streams: list[Path], fallback: str
) -> str:
    if explicit:
        return explicit
    if reply_to:
        for stream in streams:
            for msg in read_jsonl(stream):
                if msg.get("id") == reply_to:
                    thread = msg.get("thread")
                    if isinstance(thread, str) and thread:
                        return thread
        return reply_to
    return fallback


def find_message(message_id: str | None, streams: list[Path]) -> dict[str, Any] | None:
    if not message_id:
        return None
    for stream in streams:
        for msg in read_jsonl(stream):
            if msg.get("id") == message_id:
                return msg
    return None


def infer_to_session(
    reply_to: str | None,
    explicit: str | None,
    broadcast: bool,
    streams: list[Path],
) -> str | None:
    if broadcast:
        if explicit is not None:
            raise HandoffError("--broadcast cannot be combined with --to-session")
        return None
    if explicit is not None:
        error = validate_session_id(explicit, "to_session")
        if error:
            raise HandoffError(error)
        return explicit
    original = find_message(reply_to, streams)
    if original:
        value = original.get("from_session")
        if isinstance(value, str) and value:
            error = validate_session_id(value, "to_session")
            if error:
                raise HandoffError(error)
            return value
    return None


def read_context(args: argparse.Namespace) -> str | None:
    parts: list[str] = []
    if args.context:
        parts.append(args.context)
    if args.context_file:
        path = Path(args.context_file)
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise HandoffError(f"--context-file cannot be read: {exc}") from exc
    if args.context_stdin:
        parts.append(sys.stdin.read())
    if not parts:
        return None
    return "\n\n".join(parts)


def parse_skipped(values: list[str] | None) -> list[dict[str, str]] | None:
    if not values:
        return None
    parsed: list[dict[str, str]] = []
    for value in values:
        text = value.strip()
        if text.startswith("{"):
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise HandoffError(f"--skipped JSON is invalid: {exc}") from exc
            if not isinstance(item, dict):
                raise HandoffError("--skipped JSON value must be an object")
            skipped_id = item.get("id")
            reason = item.get("reason")
        else:
            if "=" in text:
                skipped_id, reason = text.split("=", 1)
            elif ":" in text:
                skipped_id, reason = text.split(":", 1)
            else:
                raise HandoffError("--skipped must be JSON or ID=REASON")
        if skipped_id is None or reason is None:
            raise HandoffError("--skipped requires id and reason")
        skipped_id = str(skipped_id).strip()
        reason = str(reason).strip()
        if not skipped_id or not reason:
            raise HandoffError("--skipped requires non-empty id and reason")
        parsed.append({"id": skipped_id, "reason": reason})
    return parsed


def truncate_summary(summary: str, note_rel: str) -> str:
    """Shrink `summary` to fit the single-line limit, pointing at the note."""
    suffix = f"... see {note_rel}"
    room = max(SUMMARY_MIN_ROOM, SUMMARY_SPILL - len(suffix))
    return summary[:room].rstrip() + suffix


def validate_notes_file(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return "refs.notes_file must be a non-empty string or null"
    if "\\" in value:
        return "refs.notes_file must use POSIX '/' separators"
    if ":" in value:
        return "refs.notes_file must be relative and cannot contain a drive or scheme"
    path = PurePosixPath(value)
    if path.is_absolute():
        return "refs.notes_file must be relative to .handoff-runtime"
    parts = path.parts
    if not parts or parts[0] != "notes":
        return "refs.notes_file must be under notes/"
    if any(part in {"", ".", ".."} for part in parts):
        return "refs.notes_file cannot contain empty, '.', or '..' path segments"
    return None


def resolve_note_path(runtime: Path, value: Any) -> Path:
    error = validate_notes_file(value)
    if error:
        raise HandoffError(error)
    candidate = (runtime / str(value)).resolve()
    notes_root = (runtime / "notes").resolve()
    try:
        candidate.relative_to(notes_root)
    except ValueError as exc:
        raise HandoffError("refs.notes_file escapes .handoff-runtime/notes") from exc
    return candidate


def validate_message(msg: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    required = [
        "v",
        "id",
        "ts",
        "from",
        "type",
        "thread",
        "summary",
        "blocking",
        "refs",
    ]
    for key in required:
        if key not in msg:
            errors.append(f"missing {key}")
    if msg.get("v") != "1.0":
        errors.append("v must be 1.0")
    if parse_msg_seq(msg.get("id")) is None:
        errors.append("id is invalid")
    elif not str(msg.get("id")).startswith(f"{msg.get('from')}-"):
        errors.append("id side does not match from")
    if msg.get("from") not in {"codex", "claude"}:
        errors.append("from is invalid")
    for field in ["from_session", "to_session"]:
        error = validate_session_id(msg.get(field), field)
        if error:
            errors.append(error)
    if msg.get("type") not in VALID_TYPES:
        errors.append("type is invalid")
    if (
        not isinstance(msg.get("thread"), str)
        or parse_msg_seq(msg.get("thread")) is None
    ):
        errors.append("thread is invalid")
    summary = msg.get("summary")
    if not isinstance(summary, str):
        errors.append("summary must be a string")
    else:
        if "\n" in summary or "\r" in summary:
            errors.append("summary must be single-line")
        if len(summary) > SUMMARY_MAX:
            errors.append(f"summary exceeds {SUMMARY_MAX} characters")
        elif len(summary) > SUMMARY_SPILL:
            warnings.append(f"summary is longer than {SUMMARY_SPILL} characters")
    if not isinstance(msg.get("blocking"), bool):
        errors.append("blocking must be boolean")
    refs_obj = msg.get("refs")
    refs: dict[str, Any] = refs_obj if isinstance(refs_obj, dict) else {}
    if not isinstance(refs_obj, dict):
        errors.append("refs must be an object")
    else:
        for key in ["reply_to", "notes_file", "commit"]:
            if key not in refs:
                errors.append(f"refs.{key} missing")
    note_error = validate_notes_file(refs.get("notes_file"))
    if note_error:
        errors.append(note_error)
    msg_type = msg.get("type")
    if msg_type in {"done", "cancel"} and not refs.get("reply_to"):
        errors.append(f"{msg_type} requires refs.reply_to")
    if msg_type == "task" and not (msg.get("goal") or msg.get("next_action")):
        errors.append("task requires goal or next_action")
    if msg_type == "handoff" and not msg.get("next_action"):
        errors.append("handoff requires next_action")
    if "state" in msg and msg_type not in {"status", "done"}:
        errors.append("state is only valid on status or done")
    if "state" in msg and msg.get("state") not in VALID_STATES:
        errors.append("state is invalid")
    for field in ["applied", "skipped", "total_proposed"]:
        if field in msg and msg_type != "done":
            errors.append(f"{field} is only valid on done")
    for field in ["applied", "total_proposed"]:
        value = msg.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            errors.append(f"{field} must be a non-negative integer")
    skipped = msg.get("skipped")
    if skipped is not None:
        if not isinstance(skipped, list):
            errors.append("skipped must be an array")
        else:
            for index, item in enumerate(skipped):
                if not isinstance(item, dict):
                    errors.append(f"skipped[{index}] must be an object")
                    continue
                skipped_id = item.get("id")
                reason = item.get("reason")
                if not isinstance(skipped_id, str) or not skipped_id:
                    errors.append(f"skipped[{index}].id must be a non-empty string")
                if not isinstance(reason, str) or not reason:
                    errors.append(f"skipped[{index}].reason must be a non-empty string")
                elif "\n" in reason or "\r" in reason or len(reason) > SUMMARY_MAX:
                    errors.append(
                        f"skipped[{index}].reason must be single-line and <={SUMMARY_MAX} chars"
                    )
    for field in ["expected_within", "eta"]:
        value = msg.get(field)
        if value is not None and not DURATION_RE.match(str(value)):
            errors.append(f"{field} must be ISO 8601 duration")
    return errors, warnings


def duplicate_warnings(outbox: Path, msg: dict[str, Any]) -> list[str]:
    """PROTOCOL.md 5 / 6.3: warn when a terminal reply would be re-sent."""
    reply_to = msg.get("refs", {}).get("reply_to")
    if not reply_to or msg.get("type") not in TERMINAL_TYPES:
        return []
    hits: list[str] = []
    for old in read_jsonl(outbox)[-DUPLICATE_SCAN_WINDOW:]:
        if (
            old.get("type") == msg.get("type")
            and old.get("refs", {}).get("reply_to") == reply_to
        ):
            hits.append(str(old.get("id")))
    if not hits:
        return []
    return [f"recent duplicate {msg.get('type')} for {reply_to}: {', '.join(hits)}"]


def build_message(
    args: argparse.Namespace, runtime: Path
) -> tuple[dict[str, Any], Path, Path, str | None]:
    seq_path, outbox, peer_stream = side_paths(runtime, args.side)
    next_seq = next_local_seq(seq_path, outbox)
    msg_id = f"{args.side}-{next_seq:06d}"
    streams = [outbox, peer_stream]
    context = read_context(args)
    refs = {
        "reply_to": args.reply_to,
        "notes_file": args.notes_file,
        "commit": args.commit,
    }
    summary = args.summary
    note_text: str | None = None
    if len(summary) > SUMMARY_SPILL:
        # Spill the full text into a note so nothing is silently dropped.
        note_rel = refs["notes_file"] or f"notes/{msg_id}.md"
        note_text = "# Original summary\n\n" + summary + "\n"
        if context:
            note_text += "\n# Context\n\n" + context.rstrip() + "\n"
            context = None
        refs["notes_file"] = note_rel
        summary = truncate_summary(summary, note_rel)
    elif args.notes_file and not Path(runtime / args.notes_file).is_file():
        # Referencing a note that does not exist leaves the peer with a dead
        # ref, which doctor.py would only report much later with no cause.
        print(
            f"WARN: refs.notes_file {args.notes_file} does not exist yet; "
            "write it before sending, or drop the flag",
            file=sys.stderr,
        )
    thread = infer_thread(args.reply_to, args.thread, streams, msg_id)
    from_session = resolve_session_id(args.side, args.session, runtime)
    to_session = infer_to_session(
        args.reply_to, args.to_session, args.broadcast, streams
    )
    msg: dict[str, Any] = {
        "v": "1.0",
        "id": msg_id,
        "ts": now_iso(),
        "from": args.side,
        "from_session": from_session,
        "type": args.type,
        "thread": thread,
        "summary": summary,
        "blocking": args.blocking,
        "peer_cursor_observed": args.peer_cursor_observed
        if args.peer_cursor_observed is not None
        else max_seq(peer_stream),
        "refs": refs,
    }
    if to_session:
        msg["to_session"] = to_session
    optional_fields = {
        "context": context,
        "next_action": args.next_action,
        "goal": args.goal,
        "priority": args.priority,
        "expected_within": args.expected_within,
        "state": args.state,
        "eta": args.eta,
        "applied": args.applied,
        "skipped": parse_skipped(args.skipped),
        "total_proposed": args.total_proposed,
    }
    for key, value in optional_fields.items():
        if value is not None:
            msg[key] = value
    for key, values in [
        ("acceptance", args.acceptance),
        ("constraints", args.constraint),
        ("context_files", args.context_file_ref),
        ("files_changed", args.file_changed),
    ]:
        if values:
            msg[key] = values
    return msg, seq_path, outbox, note_text


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send one .handoff JSONL message")
    parser.add_argument("--side", required=True, choices=["claude", "codex"])
    parser.add_argument("--type", required=True, choices=sorted(VALID_TYPES))
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--session",
        help="Current session id. Defaults to env or .handoff-runtime/.<side>-session.",
    )
    parser.add_argument(
        "--to-session", help="Direct this message to a specific peer session."
    )
    parser.add_argument(
        "--broadcast",
        action="store_true",
        help="Do not infer to_session from --reply-to.",
    )
    parser.add_argument("--thread")
    parser.add_argument("--reply-to")
    parser.add_argument("--notes-file")
    parser.add_argument("--commit")
    parser.add_argument("--context")
    parser.add_argument("--context-file")
    parser.add_argument("--context-stdin", action="store_true")
    parser.add_argument("--next-action")
    parser.add_argument("--goal")
    parser.add_argument("--acceptance", action="append")
    parser.add_argument("--constraint", action="append")
    parser.add_argument("--context-file-ref", action="append")
    parser.add_argument("--file-changed", action="append")
    parser.add_argument("--priority", choices=["urgent", "normal", "backlog"])
    parser.add_argument("--blocking", action="store_true")
    parser.add_argument("--expected-within")
    parser.add_argument("--state", choices=sorted(VALID_STATES))
    parser.add_argument("--eta")
    parser.add_argument("--applied", type=int)
    parser.add_argument(
        "--skipped", action="append", help="Repeatable. JSON object or ID=REASON."
    )
    parser.add_argument("--total-proposed", type=int)
    parser.add_argument("--peer-cursor-observed", type=int)
    parser.add_argument("--dry-run", "--verify", dest="dry_run", action="store_true")
    return parser.parse_args(argv)


def write_message(args: argparse.Namespace, runtime: Path, dry_run: bool) -> int:
    msg, seq_path, outbox, note_text = build_message(args, runtime)
    errors, warnings = validate_message(msg)
    warnings.extend(duplicate_warnings(outbox, msg))
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"FATAL: {error}", file=sys.stderr)
        print(json.dumps(msg, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(msg, ensure_ascii=False, indent=2))
    if dry_run:
        print(f"DRY-RUN: would write {msg['id']} to {outbox}")
        if note_text:
            print(f"DRY-RUN: would write {msg['refs']['notes_file']}")
        return 0
    if note_text:
        note_path = resolve_note_path(runtime, msg["refs"]["notes_file"])
        if not write_new_file(note_path, note_text):
            # PROTOCOL.md 6.3: a replay reuses the note instead of failing.
            print(
                f"WARN: note {msg['refs']['notes_file']} already exists; reusing it",
                file=sys.stderr,
            )
    append_jsonl(outbox, msg)
    atomic_write_text(seq_path, f"{int(msg['id'].rsplit('-', 1)[1])}\n")
    print(f"WROTE {msg['id']} to {outbox}")
    return 0


def main(argv: list[str]) -> int:
    utf8_stdout()
    args = parse_args(argv)
    root = find_project_root(Path.cwd())
    runtime = runtime_dir(root)
    if args.dry_run:
        return write_message(args, runtime, dry_run=True)
    with SendLock(runtime, args.side):
        return write_message(args, runtime, dry_run=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except HandoffError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(2)
