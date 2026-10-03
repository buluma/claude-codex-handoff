#!/usr/bin/env python3
"""Unit tests for the claude-codex-handoff tools.

Run from the repository root:

    python -m unittest discover -s tools/tests -v

These cover the invariants that are pure functions of runtime state: cursor
resolution, schema validation, stream partitioning, and gate classification.
The cursor tests are regression tests for the bug where poll-gate.py looked for
`cursors/claude-claude-main` while doctor.py read `cursors/claude-main`, so the
gate reported "idle" with real work queued and the model was never woken.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import _common  # noqa: E402
import archive  # noqa: E402
import send  # noqa: E402


def load_tool(module_name: str, filename: str):
    """Import a tool whose filename is not a valid module name (poll-gate.py)."""
    spec = importlib.util.spec_from_file_location(module_name, TOOLS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


poll_gate = load_tool("poll_gate", "poll-gate.py")


class RuntimeFixture:
    """A throwaway project root with a `.handoff-runtime/` in v1.9 layout."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.handoff = self.root / ".handoff"
        self.handoff.mkdir()
        self.runtime = self.root / ".handoff-runtime"
        for sub in ("notes", "claims", "locks", "cursors", "archive"):
            (self.runtime / sub).mkdir(parents=True)
        for name in ("claude-to-codex.jsonl", "codex-to-claude.jsonl"):
            (self.runtime / name).write_text("", encoding="utf-8")
        for name in (".claude-seq", ".codex-seq", ".claude-cursor", ".codex-cursor"):
            (self.runtime / name).write_text("0\n", encoding="utf-8")

    def cleanup(self) -> None:
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------
    def write_cursor(self, name: str, value: int) -> None:
        (self.runtime / "cursors" / name).write_text(f"{value}\n", encoding="utf-8")

    def write_legacy(self, side: str, value: int) -> None:
        (self.runtime / f".{side}-cursor").write_text(f"{value}\n", encoding="utf-8")

    def append(self, filename: str, msg: dict) -> None:
        with (self.runtime / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def append_raw(self, filename: str, line: str) -> None:
        with (self.runtime / filename).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_stream(self, filename: str) -> list[dict]:
        out = []
        for line in (self.runtime / filename).read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out


def message(seq: int, side: str, **overrides) -> dict:
    """A minimal schema-valid message, per PROTOCOL.md section 3."""
    msg = {
        "v": "1.0",
        "id": f"{side}-{seq:06d}",
        "ts": "2026-06-17T09:00:00.000Z",
        "from": side,
        "type": "status",
        "thread": f"{side}-000001",
        "summary": "test message",
        "blocking": False,
        "refs": {"reply_to": None, "notes_file": None, "commit": None},
    }
    msg.update(overrides)
    return msg


class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RuntimeFixture()
        self.addCleanup(self.fx.cleanup)


# ----------------------------------------------------------------------
# Cursor resolution -- the P0 regression tests
# ----------------------------------------------------------------------


class TestCursorResolution(FixtureTest):
    def test_session_id_carrying_side_prefix_is_not_doubled(self):
        # PROTOCOL.md 3.1 documents session ids like `claude-main` and a
        # `<side>-default` fallback, so prefixing again would look for
        # `claude-claude-main` and miss the file the agent actually wrote.
        self.assertEqual(
            _common.cursor_filename("claude", "claude-main"), "claude-main"
        )
        self.assertEqual(_common.cursor_filename("codex", "codex-main"), "codex-main")
        self.assertEqual(
            _common.cursor_filename("claude", "claude-default"), "claude-default"
        )

    def test_bare_session_id_is_prefixed_per_protocol(self):
        self.assertEqual(_common.cursor_filename("claude", "main"), "claude-main")
        self.assertEqual(_common.cursor_filename("codex", "cron"), "codex-cron")

    def test_resolves_cursor_written_under_either_convention(self):
        self.fx.write_cursor("claude-main", 7)
        got = _common.resolve_cursor(self.fx.runtime, "claude", "claude-main")
        self.assertEqual(got.value, 7)
        self.assertEqual(got.source, "cursor")
        self.assertEqual(got.used_filename, "claude-main")

        # The pre-fix doubled spelling must keep working: existing runtimes
        # should not need migration.
        self.fx.write_cursor("codex-codex-main", 4)
        got = _common.resolve_cursor(self.fx.runtime, "codex", "codex-main")
        self.assertEqual(got.value, 4)
        self.assertEqual(got.used_filename, "codex-codex-main")

    def test_prefers_own_cursor_over_legacy_anchor(self):
        # A high legacy anchor must not mask a real session cursor: doing so
        # is exactly how pending work got silently skipped.
        self.fx.write_legacy("claude", 500)
        self.fx.write_cursor("claude-main", 2)
        got = _common.resolve_cursor(self.fx.runtime, "claude", "claude-main")
        self.assertEqual(got.value, 2)
        self.assertEqual(got.source, "cursor")

    def test_falls_back_to_legacy_anchor_then_zero(self):
        self.fx.write_legacy("claude", 3)
        got = _common.resolve_cursor(self.fx.runtime, "claude", "claude-main")
        self.assertEqual((got.value, got.source), (3, "legacy"))

        # With no anchor at all the cursor is an honest 0, not a guess.
        (self.fx.runtime / ".codex-cursor").unlink()
        got = _common.resolve_cursor(self.fx.runtime, "codex", "codex-main")
        self.assertEqual((got.value, got.source), (0, "default"))

    def test_write_cursor_lands_on_a_file_the_resolver_finds(self):
        for side, session in [
            ("claude", "claude-main"),
            ("claude", "main"),
            ("codex", "codex-thread-019e4408"),
            ("codex", "reviewer"),
        ]:
            with self.subTest(session=session):
                path = _common.write_cursor(self.fx.runtime, side, session, 9)
                got = _common.resolve_cursor(self.fx.runtime, side, session)
                self.assertEqual(got.value, 9, f"round-trip failed for {session}")
                self.assertTrue(path.is_file())
                self.assertEqual(got.used_filename, path.name)

    def test_corrupt_cursor_does_not_silently_read_as_zero(self):
        self.fx.write_cursor("claude-main", 5)
        (self.fx.runtime / "cursors" / "claude-main").write_text(
            "junk", encoding="utf-8"
        )
        # Unreadable -> treated as absent, so the legacy anchor still applies
        # rather than reporting a confident wrong 0.
        self.assertIsNone(_common.read_int(self.fx.runtime / "cursors" / "claude-main"))


class TestCursorResolutionAgreesAcrossTools(FixtureTest):
    def test_gate_and_doctor_resolve_the_same_cursor(self):
        """The regression that mattered: two tools, two answers, lost work."""
        self.fx.append(
            "codex-to-claude.jsonl", message(1, "codex", type="task", goal="g")
        )
        self.fx.write_cursor("claude-main", 0)

        got = _common.resolve_cursor(self.fx.runtime, "claude", "claude-main")
        self.assertEqual(got.value, 0)
        self.assertEqual(got.used_filename, "claude-main")

        counts = poll_gate.classify(
            poll_gate.read_inbound(self.fx.runtime, "claude")[1],
            got.value,
            "claude-main",
        )
        self.assertEqual(counts["for_me_lease"], 1)
        decision, code = poll_gate.decide(
            counts, self.fx.runtime, "claude", "claude-main", 0
        )
        self.assertEqual((decision, code), ("process", poll_gate.EXIT_PROCESS))


# ----------------------------------------------------------------------
# Message schema validation
# ----------------------------------------------------------------------


class TestValidateMessage(FixtureTest):
    def check(self, msg: dict) -> list[str]:
        errors, _warnings = send.validate_message(msg)
        return errors

    def test_minimal_message_is_valid(self):
        self.assertEqual(self.check(message(1, "claude")), [])

    def test_requires_v_1_0(self):
        self.assertIn("v must be 1.0", self.check(message(1, "claude", v="2.0")))

    def test_rejects_id_side_mismatch(self):
        errors = self.check(message(1, "codex", **{"from": "claude"}))
        self.assertIn("id side does not match from", errors)

    def test_rejects_bad_session_id(self):
        self.assertTrue(
            any(
                "from_session" in e
                for e in self.check(message(1, "claude", from_session="bad id"))
            )
        )

    def test_done_requires_reply_to(self):
        self.assertIn(
            "done requires refs.reply_to", self.check(message(1, "claude", type="done"))
        )
        ok = message(
            1,
            "claude",
            type="done",
            refs={"reply_to": "codex-000001", "notes_file": None, "commit": None},
        )
        self.assertEqual(self.check(ok), [])

    def test_handoff_requires_next_action(self):
        self.assertIn(
            "handoff requires next_action",
            self.check(message(1, "claude", type="handoff")),
        )

    def test_task_requires_goal_or_next_action(self):
        self.assertIn(
            "task requires goal or next_action",
            self.check(message(1, "claude", type="task")),
        )

    def test_rejects_multiline_and_oversized_summary(self):
        self.assertIn(
            "summary must be single-line",
            self.check(message(1, "claude", summary="a\nb")),
        )
        long = message(1, "claude", summary="x" * 201)
        self.assertIn(
            f"summary exceeds {send.SUMMARY_MAX} characters", self.check(long)
        )

    def test_warns_in_the_spill_band_without_failing(self):
        msg = message(1, "claude", summary="x" * 190)
        errors, warnings = send.validate_message(msg)
        self.assertEqual(errors, [])
        self.assertTrue(any("longer than" in w for w in warnings))

    def test_state_only_on_status_or_done(self):
        self.assertIn(
            "state is only valid on status or done",
            self.check(message(1, "claude", type="task", goal="g", state="claimed")),
        )

    def test_audit_fields_only_on_done(self):
        self.assertIn(
            "applied is only valid on done", self.check(message(1, "claude", applied=1))
        )

    def test_notes_file_must_stay_under_notes(self):
        for bad in (
            "/etc/passwd",
            "../escape.md",
            "notes/../../escape.md",
            "other/x.md",
            "notes\\x.md",
        ):
            with self.subTest(bad=bad):
                msg = message(
                    1,
                    "claude",
                    refs={"reply_to": None, "notes_file": bad, "commit": None},
                )
                self.assertTrue(self.check(msg), f"{bad} should be rejected")

    def test_iso8601_durations(self):
        msg = message(1, "claude", expected_within="PT2H")
        self.assertEqual(self.check(msg), [])
        self.assertIn(
            "expected_within must be ISO 8601 duration",
            self.check(message(1, "claude", expected_within="2 hours")),
        )


class TestSendHelpers(FixtureTest):
    def test_truncate_summary_points_at_the_note(self):
        out = send.truncate_summary("y" * 400, "notes/claude-000001.md")
        self.assertLessEqual(len(out), send.SUMMARY_SPILL)
        self.assertTrue(out.endswith("... see notes/claude-000001.md"))

    def test_parse_skipped_accepts_both_spellings(self):
        parsed = send.parse_skipped(["a=b", '{"id":"c","reason":"d"}'])
        self.assertEqual(
            parsed, [{"id": "a", "reason": "b"}, {"id": "c", "reason": "d"}]
        )

    def test_parse_skipped_rejects_garbage(self):
        with self.assertRaises(_common.HandoffError):
            send.parse_skipped(["noreason"])

    def test_corrupt_seq_file_raises_a_clean_error(self):
        (self.fx.runtime / ".claude-seq").write_text("not-a-number", encoding="utf-8")
        with self.assertRaises(_common.HandoffError):
            _common.read_seq_file(self.fx.runtime / ".claude-seq")

    def test_next_local_seq_recovers_from_reset_seq_file(self):
        for seq in range(1, 4):
            self.fx.append("claude-to-codex.jsonl", message(seq, "claude"))
        (self.fx.runtime / ".claude-seq").write_text("0\n", encoding="utf-8")
        self.assertEqual(
            send.next_local_seq(
                self.fx.runtime / ".claude-seq",
                self.fx.runtime / "claude-to-codex.jsonl",
            ),
            4,
        )


# ----------------------------------------------------------------------
# Gate classification
# ----------------------------------------------------------------------


class TestGateClassify(FixtureTest):
    def msgs(self, *specs):
        return [message(seq, "codex", **kw) for seq, kw in specs]

    def test_only_counts_messages_above_cursor(self):
        counts = poll_gate.classify(
            self.msgs((1, {}), (2, {}), (3, {})), 2, "claude-main"
        )
        self.assertEqual(counts["unread_total"], 1)

    def test_lease_versus_pure_split(self):
        counts = poll_gate.classify(
            self.msgs(
                (1, {"type": "task", "goal": "g"}),
                (
                    2,
                    {
                        "type": "done",
                        "refs": {
                            "reply_to": "claude-000001",
                            "notes_file": None,
                            "commit": None,
                        },
                    },
                ),
            ),
            0,
            "claude-main",
        )
        self.assertEqual(counts["for_me_lease"], 1)
        self.assertEqual(counts["for_me_pure"], 1)

    def test_message_for_another_session_is_not_for_me(self):
        counts = poll_gate.classify(
            self.msgs((1, {"type": "task", "goal": "g", "to_session": "claude-other"})),
            0,
            "claude-main",
        )
        self.assertEqual(counts["directed_elsewhere"], 1)
        self.assertEqual(counts["for_me_lease"], 0)
        self.assertEqual(
            poll_gate.decide(counts, self.fx.runtime, "claude", "claude-main", 0)[0],
            "idle",
        )

    def test_message_addressed_to_me_counts(self):
        counts = poll_gate.classify(
            self.msgs((1, {"type": "task", "goal": "g", "to_session": "claude-main"})),
            0,
            "claude-main",
        )
        self.assertEqual(counts["for_me_lease"], 1)

    def test_unknown_type_wakes_but_is_visible(self):
        counts = poll_gate.classify(
            self.msgs((1, {"type": "invented"})), 0, "claude-main"
        )
        self.assertEqual(counts["unknown_type"], 1)
        self.assertEqual(
            poll_gate.decide(counts, self.fx.runtime, "claude", "claude-main", 0)[0],
            "process",
        )


class TestProactiveStreak(FixtureTest):
    def test_streak_fires_once_every_n_idle_ticks(self):
        for expected_ticks in range(1, 7):
            with self.subTest(tick=expected_ticks):
                decision, code = poll_gate.decide(
                    {
                        "for_me_lease": 0,
                        "for_me_pure": 0,
                        "unknown_type": 0,
                        "unread_total": 0,
                        "directed_elsewhere": 0,
                    },
                    self.fx.runtime,
                    "claude",
                    "claude-main",
                    3,
                )
                if expected_ticks % 3 == 0:
                    self.assertEqual(
                        (decision, code), ("proactive", poll_gate.EXIT_PROACTIVE)
                    )
                else:
                    self.assertEqual((decision, code), ("idle", poll_gate.EXIT_IDLE))

    def test_work_resets_the_streak(self):
        empty = {
            "for_me_lease": 0,
            "for_me_pure": 0,
            "unknown_type": 0,
            "unread_total": 0,
            "directed_elsewhere": 0,
        }
        poll_gate.decide(empty, self.fx.runtime, "claude", "claude-main", 3)
        poll_gate.decide(empty, self.fx.runtime, "claude", "claude-main", 3)
        poll_gate.decide(
            {
                "for_me_lease": 1,
                "for_me_pure": 0,
                "unknown_type": 0,
                "unread_total": 1,
                "directed_elsewhere": 0,
            },
            self.fx.runtime,
            "claude",
            "claude-main",
            3,
        )
        self.assertEqual(
            _common.read_int(self.fx.runtime / ".claude-pollgate.json") or 0, 0
        )
        self.assertEqual(
            poll_gate.load_proactive_streak(self.fx.runtime, "claude", "claude-main"), 0
        )


# ----------------------------------------------------------------------
# Archival
# ----------------------------------------------------------------------


class TestArchivePartition(FixtureTest):
    def write_stream(self, *seqs, raw: list[str] | None = None):
        for seq in seqs:
            self.fx.append("codex-to-claude.jsonl", message(seq, "codex"))
        for line in raw or []:
            self.fx.append_raw("codex-to-claude.jsonl", line)

    def test_archives_consumed_prefix_but_never_newest(self):
        self.write_stream(1, 2, 3, 4, 5)
        to_archive, retained, bad, newest = archive.partition(
            self.fx.runtime / "codex-to-claude.jsonl", 3
        )
        self.assertEqual(len(to_archive), 3)  # seqs 1-3
        self.assertEqual(len(retained), 2)  # seqs 4-5
        self.assertEqual(newest, 5)
        self.assertEqual(bad, [])

    def test_point_past_newest_archives_all_but_newest(self):
        self.write_stream(1, 2, 3)
        to_archive, retained, _bad, _newest = archive.partition(
            self.fx.runtime / "codex-to-claude.jsonl", 99
        )
        self.assertEqual(len(to_archive), 2)
        self.assertEqual(len(retained), 1)

    def test_unparseable_line_is_reported_not_guessed(self):
        self.write_stream(1, 2, raw=["not json at all"])
        to_archive, retained, bad, _newest = archive.partition(
            self.fx.runtime / "codex-to-claude.jsonl", 2
        )
        self.assertEqual(bad, [3])
        self.assertIn("not json at all", retained)  # kept, never archived
        self.assertNotIn("not json at all", to_archive)

    def test_partitioned_records_rejoin_without_blank_lines(self):
        # Archive rewrites the live stream, so a stray blank line between
        # records would corrupt every downstream line number.
        self.write_stream(1, 2, 3)
        to_archive, retained, _bad, _newest = archive.partition(
            self.fx.runtime / "codex-to-claude.jsonl", 2
        )
        for group in (to_archive, retained):
            for line in group:
                self.assertEqual(line, line.strip())
            self.assertNotIn("", group)

    def test_fully_corrupt_stream_does_not_crash(self):
        # Pre-fix this raised ValueError: max() iterable argument is empty.
        (self.fx.runtime / "codex-to-claude.jsonl").write_text(
            "garbage\nmore garbage\n", encoding="utf-8"
        )
        to_archive, retained, bad, newest = archive.partition(
            self.fx.runtime / "codex-to-claude.jsonl", 5
        )
        self.assertEqual(newest, None)
        self.assertEqual(len(retained), 2)
        self.assertEqual(len(bad), 2)

    def test_empty_stream_does_not_crash(self):
        to_archive, retained, bad, newest = archive.partition(
            self.fx.runtime / "codex-to-claude.jsonl", 5
        )
        self.assertEqual((to_archive, retained, bad, newest), ([], [], [], None))

    def test_archive_point_is_the_minimum_reader_cursor(self):
        self.fx.write_cursor("claude-a", 5)
        self.fx.write_cursor("claude-b", 2)
        self.assertEqual(archive.reader_archive_point(self.fx.runtime, "claude"), 2)

    def test_no_reader_cursor_means_no_archiving(self):
        self.fx.runtime.joinpath(".claude-cursor").unlink()
        self.assertIsNone(archive.reader_archive_point(self.fx.runtime, "claude"))


# ----------------------------------------------------------------------
# End-to-end through the real CLIs
# ----------------------------------------------------------------------


def run_tool(
    fixture: RuntimeFixture, tool: str, *args: str
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in list(env):
        if key.endswith("_SESSION_ID") or key == "HANDOFF_SESSION_ID":
            del env[key]
    return subprocess.run(
        [sys.executable, str(TOOLS_DIR / tool), *args],
        cwd=fixture.root,
        capture_output=True,
        text=True,
        env=env,
    )


class TestStreamDirection(FixtureTest):
    """Each side writes its own stream and polls the other one."""

    def test_side_paths_direction(self):
        seq, outbox, peer = _common.side_paths(self.fx.runtime, "claude")
        self.assertEqual(seq.name, ".claude-seq")
        self.assertEqual(outbox.name, "claude-to-codex.jsonl")
        self.assertEqual(peer.name, "codex-to-claude.jsonl")

        seq, outbox, peer = _common.side_paths(self.fx.runtime, "codex")
        self.assertEqual(seq.name, ".codex-seq")
        self.assertEqual(outbox.name, "codex-to-claude.jsonl")
        self.assertEqual(peer.name, "claude-to-codex.jsonl")

    def test_inbound_matches_the_peer_outbox(self):
        for side in ("claude", "codex"):
            peer_side = "codex" if side == "claude" else "claude"
            _seq, _outbox, peer = _common.side_paths(self.fx.runtime, side)
            self.assertEqual(
                peer.name, _common.OUTBOUND[peer_side], f"{side} polls the wrong stream"
            )
            self.assertEqual(_common.INBOUND[side][0], peer.name)

    def test_claude_send_lands_in_the_claude_stream(self):
        # An inverted mapping here appends to the peer's stream, which corrupts
        # the protocol in a way that is very hard to notice later.
        run_tool(
            self.fx,
            "send.py",
            "--side",
            "claude",
            "--type",
            "status",
            "--summary",
            "hi",
        )
        self.assertEqual(len(self.fx.read_stream("claude-to-codex.jsonl")), 1)
        self.assertEqual(self.fx.read_stream("codex-to-claude.jsonl"), [])

    def test_peer_cursor_observed_reads_the_peer_stream(self):
        run_tool(
            self.fx,
            "send.py",
            "--side",
            "codex",
            "--type",
            "status",
            "--summary",
            "one",
        )
        run_tool(
            self.fx,
            "send.py",
            "--side",
            "claude",
            "--type",
            "status",
            "--summary",
            "two",
        )
        msg = self.fx.read_stream("claude-to-codex.jsonl")[0]
        self.assertEqual(msg["peer_cursor_observed"], 1)


class TestCommandLine(FixtureTest):
    def test_send_writes_without_the_stray_empty_files_changed_field(self):
        # Pre-fix a for/else bug put files_changed: [] on every single message.
        result = run_tool(
            self.fx,
            "send.py",
            "--side",
            "claude",
            "--type",
            "status",
            "--summary",
            "hi",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        msg = self.fx.read_stream("claude-to-codex.jsonl")[0]
        self.assertNotIn("files_changed", msg)
        self.assertEqual(msg["from_session"], "claude-default")

    def test_send_rejects_an_invalid_session_with_a_clean_error(self):
        result = run_tool(
            self.fx,
            "send.py",
            "--side",
            "claude",
            "--type",
            "status",
            "--summary",
            "hi",
            "--session",
            "bad session!",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("FATAL:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_send_reuses_an_existing_note_instead_of_crashing(self):
        notes = self.fx.runtime / "notes"
        notes.mkdir(exist_ok=True)
        (notes / "x.md").write_text("first\n", encoding="utf-8")
        result = run_tool(
            self.fx,
            "send.py",
            "--side",
            "claude",
            "--type",
            "status",
            "--summary",
            "z" * 250,
            "--notes-file",
            "notes/x.md",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((notes / "x.md").read_text(encoding="utf-8"), "first\n")

    def test_gate_wakes_when_a_task_is_pending(self):
        run_tool(
            self.fx,
            "send.py",
            "--side",
            "codex",
            "--type",
            "task",
            "--goal",
            "g",
            "--summary",
            "please",
        )
        self.fx.write_cursor("claude-main", 0)
        result = run_tool(
            self.fx, "poll-gate.py", "--side", "claude", "--session", "claude-main"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["decision"], "process")
        self.assertEqual(summary["cursor_file"], "claude-main")

    def test_gate_is_idle_when_nothing_is_pending(self):
        result = run_tool(
            self.fx, "poll-gate.py", "--side", "claude", "--session", "claude-main"
        )
        self.assertEqual(result.returncode, 20)
        self.assertEqual(json.loads(result.stdout)["decision"], "idle")

    def test_gate_rejects_an_invalid_session_instead_of_substituting_one(self):
        result = run_tool(
            self.fx, "poll-gate.py", "--side", "claude", "--session", "bad session!"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["decision"], "error")

    def test_doctor_flags_conflicting_cursors_for_one_session(self):
        # Session id `main` canonicalises to cursors/claude-main; the doubled
        # path is a legacy spelling. When both hold different values one is
        # silently ignored, so the gate and the session drift apart.
        self.fx.write_cursor("claude-main", 0)
        self.fx.write_cursor("claude-claude-main", 9)
        result = run_tool(self.fx, "doctor.py")
        self.assertIn("conflicting cursors", result.stdout)
        self.assertEqual(result.returncode, 0)  # warning, not error

    def test_doctor_accepts_matching_cursors_under_either_spelling(self):
        self.fx.write_cursor("claude-main", 4)
        self.fx.write_cursor("claude-claude-main", 4)
        result = run_tool(self.fx, "doctor.py")
        self.assertNotIn("conflicting cursors", result.stdout)

    def test_doctor_agrees_with_the_gate_about_unread_work(self):
        run_tool(
            self.fx,
            "send.py",
            "--side",
            "codex",
            "--type",
            "task",
            "--goal",
            "g",
            "--summary",
            "please",
        )
        self.fx.write_cursor("claude-main", 0)
        doctor = run_tool(self.fx, "doctor.py").stdout
        gate = json.loads(
            run_tool(
                self.fx, "poll-gate.py", "--side", "claude", "--session", "claude-main"
            ).stdout
        )
        self.assertIn("1 unread peer message(s)", doctor)
        self.assertEqual(gate["for_me_lease"], 1)

    def test_archive_refuses_a_corrupt_stream_instead_of_crashing(self):
        (self.fx.runtime / "codex-to-claude.jsonl").write_text(
            "garbage\n", encoding="utf-8"
        )
        self.fx.write_legacy("claude", 3)
        result = run_tool(self.fx, "archive.py", "--stream", "x2c")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("refusing to archive", result.stdout)

    def test_archive_moves_only_the_consumed_prefix(self):
        for seq in range(1, 6):
            self.fx.append("codex-to-claude.jsonl", message(seq, "codex"))
        self.fx.write_cursor("claude-main", 3)
        result = run_tool(self.fx, "archive.py", "--stream", "x2c")
        self.assertEqual(result.returncode, 0, result.stderr)
        live = self.fx.read_stream("codex-to-claude.jsonl")
        self.assertEqual([m["id"] for m in live], ["codex-000004", "codex-000005"])
        archived = list((self.fx.runtime / "archive").glob("*.jsonl"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(archived[0].read_text(encoding="utf-8").splitlines()), 3)


# ----------------------------------------------------------------------
# Runtime state must not reach version control
# ----------------------------------------------------------------------


class TestRuntimeGitIgnore(FixtureTest):
    """Streams, notes, and claims are private to the two agents."""

    def make_git_work_tree(self) -> None:
        (self.fx.root / ".git").mkdir(exist_ok=True)

    def test_absent_gitignore_means_not_ignored(self):
        self.assertFalse(_common.runtime_self_ignores(self.fx.runtime))
        self.assertFalse(_common.runtime_is_git_ignored(self.fx.root, self.fx.runtime))

    def test_star_pattern_counts_as_self_ignoring(self):
        (_common.runtime_gitignore_path(self.fx.runtime)).write_text(
            "*\n", encoding="utf-8"
        )
        self.assertTrue(_common.runtime_self_ignores(self.fx.runtime))
        self.assertTrue(_common.runtime_is_git_ignored(self.fx.root, self.fx.runtime))

    def test_comments_are_skipped_when_reading_the_pattern(self):
        (_common.runtime_gitignore_path(self.fx.runtime)).write_text(
            "# runtime state is private\n*\n", encoding="utf-8"
        )
        self.assertTrue(_common.runtime_self_ignores(self.fx.runtime))

    def test_a_narrower_pattern_does_not_count_as_self_ignoring(self):
        # `*.tmp` leaves the streams tracked, so this must not be read as a
        # blanket ignore.
        (_common.runtime_gitignore_path(self.fx.runtime)).write_text(
            "*.tmp\n", encoding="utf-8"
        )
        self.assertFalse(_common.runtime_self_ignores(self.fx.runtime))

    def test_an_empty_gitignore_is_not_self_ignoring(self):
        (_common.runtime_gitignore_path(self.fx.runtime)).write_text(
            "\n# nothing here\n", encoding="utf-8"
        )
        self.assertFalse(_common.runtime_self_ignores(self.fx.runtime))

    def test_a_project_rule_counts_as_covered(self):
        (self.fx.root / ".gitignore").write_text(
            ".handoff-runtime/\nnode_modules/\n", encoding="utf-8"
        )
        self.assertTrue(_common.covered_by_project_gitignore(self.fx.root))
        self.assertTrue(_common.runtime_is_git_ignored(self.fx.root, self.fx.runtime))

    def test_an_unrelated_project_gitignore_does_not_count(self):
        (self.fx.root / ".gitignore").write_text("node_modules/\n*.pyc\n")
        self.assertFalse(_common.covered_by_project_gitignore(self.fx.root))

    def test_commented_out_project_rule_does_not_count(self):
        (self.fx.root / ".gitignore").write_text("# .handoff-runtime/\n")
        self.assertFalse(_common.covered_by_project_gitignore(self.fx.root))

    # -- doctor surfaces it --------------------------------------------

    def test_doctor_warns_when_a_work_tree_could_commit_runtime_state(self):
        self.make_git_work_tree()
        out = run_tool(self.fx, "doctor.py").stdout
        self.assertIn("is not git-ignored", out)

    def test_doctor_is_quiet_once_setup_has_run(self):
        self.make_git_work_tree()
        (_common.runtime_gitignore_path(self.fx.runtime)).write_text(
            "*\n", encoding="utf-8"
        )
        out = run_tool(self.fx, "doctor.py").stdout
        self.assertIn("is git-ignored", out)
        self.assertNotIn("is not git-ignored", out)

    def test_doctor_stays_quiet_outside_a_git_work_tree(self):
        # Nothing to leak into, so nagging would be noise.
        for path in [self.fx.root, *self.fx.root.parents]:
            if (path / ".git").exists():
                self.skipTest(f"temporary directory sits inside a work tree at {path}")
        out = run_tool(self.fx, "doctor.py").stdout
        self.assertIn("not a git work tree", out)
        self.assertNotIn("is git-ignored", out)
        self.assertNotIn("is not git-ignored", out)

    def test_doctor_does_not_flag_the_gitignore_as_runtime_cruft(self):
        (_common.runtime_gitignore_path(self.fx.runtime)).write_text(
            "*\n", encoding="utf-8"
        )
        out = run_tool(self.fx, "doctor.py").stdout
        self.assertNotIn(".gitignore: unexpected top-level runtime file", out)

    def test_strict_mode_fails_on_an_unignored_runtime(self):
        self.make_git_work_tree()
        result = run_tool(self.fx, "doctor.py", "--strict")
        self.assertEqual(result.returncode, 1, result.stdout)


class TestSetupWritesSelfIgnore(FixtureTest):
    def test_setup_sh_installs_the_self_ignoring_gitignore(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash not available")
        handoff = self.fx.handoff
        shutil.copy(TOOLS_DIR.parent / "setup.sh", handoff / "setup.sh")
        result = subprocess.run(
            [bash, str(handoff / "setup.sh")],
            cwd=self.fx.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self.fx.runtime / ".gitignore"
        self.assertTrue(marker.is_file(), "setup.sh did not create .gitignore")
        self.assertEqual(marker.read_text(encoding="utf-8"), "*\n")
        # Idempotent: a second run must not clobber a user's own file.
        marker.write_text("custom\n", encoding="utf-8")
        subprocess.run(
            [bash, str(handoff / "setup.sh")],
            cwd=self.fx.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(marker.read_text(encoding="utf-8"), "custom\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
