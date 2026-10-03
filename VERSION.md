# Changelog

Protocol documentation history for the Codex ↔ Claude collaboration kit. The current version is the title on line 1 of `PROTOCOL.md`, in two-part `MAJOR.MINOR` form:

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
