# Changelog

Protocol documentation history for the Codex ↔ Claude collaboration kit. The current version is the title on line 1 of `PROTOCOL.md`, in two-part `MAJOR.MINOR` form.

## v1.13 — 2026-10-03

Toolchain consolidation and bugfixes. The wire format is unchanged, so old runtimes keep working with no migration.

- **Fix: the wake gate and the doctor resolved per-session cursors differently.** `poll-gate.py` built `cursors/<side>-<session>` unconditionally, but §3.1 session ids already carry the side prefix (`claude-main`), so it looked for `cursors/claude-claude-main` and silently ignored `cursors/claude-main`. `doctor.py` read the unprefixed file. When both existed, the gate fell back to the legacy anchor, reported `idle`, and the model was never woken while real work sat in the queue — no error, no warning. §6.1 now states that the `<side>-` prefix is applied only when the session id does not already start with it; both spellings are accepted on read, and `doctor.py` warns when both exist with different values.
- Add `tools/_common.py`: one implementation of stream names, id grammar, session-id resolution, cursor resolution, the send lock, and durable writes. The four tools previously carried three copies of `find_project_root`, three of the atomic write, two `SendLock` classes, and two disagreeing `ID_RE` patterns.
- `send.py`: a `for`/`else` bug put `files_changed: []` on every message regardless of type. §3 scopes that field to work actually changed.
- `send.py`: report a corrupt `.<side>-seq`, an unreadable `--context-file`, and an existing `--notes-file` as clean errors instead of tracebacks. An existing note is now reused per §6.3 rather than raising `FileExistsError`.
- `archive.py`: a stream whose lines all fail to parse raised `ValueError` from `max()` on an empty sequence. It now refuses to archive and points at `doctor.py`, because a line with no parseable id cannot be proven consumed.
- `poll-gate.py`: an invalid `--session` now fails with exit `2` instead of silently substituting `<side>-default` and gating on a different session's cursor. Reports `cursor_source`, `cursor_file`, and `unknown_type` in its JSON summary.
- `doctor.py`: a missing legacy `.<side>-cursor` is informational, since the anchor is optional; unparseable stream lines are called out explicitly.
- Durable writes now fsync the parent directory after `os.replace`, so a completed rename survives power loss (§0 rule 9).
- Add `tools/tests/` and CI on Linux, macOS, and Windows.

---

Protocol documentation history prior to the tooling above. The current version is the title on line 1 of `PROTOCOL.md`, in two-part `MAJOR.MINOR` form:

- **MAJOR** — breaks backward compatibility (message field semantics change, fields are removed or changed, an old runtime needs migration, or the stream/directory format is incompatible).
- **MINOR** — backward-compatible additions (a new tool, a new optional field, a new prompt step, or a new optional flow).
- A pure documentation wording change, typo, or bugfix that does not change behavior does not bump the version.

The message schema field `v` is fixed at `"1.0"`. It does not change with the protocol document version.

---

## v1.12 — 2026-06-17

- Distinguish the idle-backoff carrier for adaptive cadence: a Codex App heartbeat keeps an any-minute `+10` backoff; a Claude recurring cron uses the expressible ladder `10 -> 20 -> 30 -> 60`, capped at 60 minutes.
- Keep the v1.11 active rule unchanged: after discovering, consuming, claiming, or resuming a new peer message, the next loop interval returns directly to 10 minutes.

## v1.11 — 2026-06-17

- Adjust adaptive cadence semantics: after discovering, consuming, claiming, or resuming a new peer message, the next loop interval returns directly to 10 minutes. Idle rounds still back off in 10-minute steps.
- Update the Codex heartbeat prompt and the Claude cron prompt together, so implementations do not keep executing "subtract 10 minutes".

## v1.10 — 2026-06-17

- Add `tools/poll-gate.py`: a deterministic wake/idle pre-gate, so each cron / heartbeat round decides "idle or process" in code rather than in the model. Exit codes are `0` process / `10` proactive / `20` idle / `2` error. It is read-only and stateless by default. `--proactive-every N` maintains a per-session idle streak (state is written to `.handoff-runtime/.<side>-pollgate.json`).
- `prompts/cron-prompt.md` and `prompts/codex-heartbeat-prompt.md` both gain a deterministic pre-gate step, and `had_peer_reply` is bound to the gate exit code.
- `tools/doctor.py` adds `.<side>-pollgate.json` to the known runtime-file allowlist.
- `PROTOCOL.md` §1 layout and §8 document the pre-gate. This `VERSION.md` is added.

## v1.9 — 2026-06-16

- First public release (initial commit). Backward-compatible additions on top of v1.8: per-session cursors (removing same-side multi-session head-of-line blocking), replay idempotency, lease renewal, stream archival, a trust boundary, optional liveness, and adaptive cadence.
- Follow-ups: the `costart` alias, an inline task goal on the trigger phrase, and autonomous-collaboration guidance.

## v1.8 and earlier

- Predates this repository's git history. It is referenced in `PROTOCOL.md` only as the baseline v1.9 evolved from, with no separate changelog retained.
