# Changelog

All notable changes to the MoA skill. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project uses semantic-ish versioning (single source: `.claude-plugin/plugin.json`, synced by `scripts/bump-version.sh`).

## [1.6.0] — 2026-07-13

> **Migration note (user-visible default change)**: the Quorum grace window is now
> **per-seat configurable**, and the shipped `config.example.yaml` raises the global
> `grace_seconds` default **30 → 90**. Why: reasoning-heavy flagship seats (Fable 5 /
> Gemini Pro) are inherently slow, so a 30s window systematically sacrifices the
> *strongest* seat's refine-round peer-vote once quorum is reached (its first-round opinion
> still lands — only its vote on others is lost, but the consensus tally then misses the
> best seat). A 90s window usually waits it back. **Revert / opt-out**: set
> `options.grace_seconds: 30` (or any value) in your config; the **script fallback stays
> 30**, so existing configs that don't set the key are unchanged. **Discoverability**:
> config comment on both `options.grace_seconds` and seat C's `grace_seconds` example, plus
> the SKILL.md status banner.

### Added
- **Per-seat `grace_seconds` override** in `dispatch_with_quorum`: each straggler's window =
  `member.get("grace_seconds", global_grace_s)`, so a high-value slow seat can be granted a
  wider window while the rest keep the global default. Windows are registered per-seat at the
  moment quorum is reached and expire independently — no single global cut-off. Backward
  compatible: seats without the field behave exactly as before. New test
  `test_dispatch_member_grace_override_survives_while_default_skips` (mixed run: one overridden
  straggler survives, one default straggler is skipped in the same call). Suite 169 → 170.

### Changed
- `config.example.yaml` global `options.grace_seconds` default **30 → 90** (see migration note);
  added a commented `grace_seconds: 150` example on seat C (Gemini) demonstrating per-seat override.

## [1.5.0] — 2026-07-13

> **Migration note (user-visible default change)**: the default committee now fields a **genuine
> fourth family** — seat D moved from a free second-Anthropic Self-MoA subagent to
> **Moonshot `kimi-k2.7` via auggie**. This fixes a correlation the v1.4.0-audit flagged: seats
> B, D **and your Claude arbiter** were all Anthropic (3 of 5 judging minds one family), so
> "everyone agrees" was weaker evidence than it looked. **Cost impact**: the default now bills
> **3 seats (A/C/D) instead of 2** — one more auggie seat at upstream API price +40%.
> **Opt-out / revert**: swap seat D back to an Opus subagent (exact block commented in
> `config.example.yaml`) to restore the cheaper v1.4.0 mix; on machines without auggie the seat
> auto-falls-back to codex. Always `dry-run` first — it now shows the 3-billed-seat estimate.

### Added
- **GitHub Actions CI** (`.github/workflows/ci.yml`): runs the test suite + `leak-check` on
  push/PR across Python 3.9 and 3.12; on release tags verifies six-place version consistency
  (`bump-version.sh --check`), that `plugin.json` version equals the tag, and that the README
  test-count badge equals the collected suite size. Closes the "quality gate was local-only /
  badge drifted 126→137→162 by hand" gap (audit F1).
- **Fourth-family default committee**: seat D = `kimi-k2.7` via auggie with a codex fallback for
  auggie-less environments (audit §7; E2E-verified: D-seat generate landed a parsed review in
  64.7s).

### Changed
- **Config-validation warning (audit F5)**: a `channel: cli` seat left on `cli_kind: auto` with a
  `model` but no `auggie_model` now prints a non-blocking warning — auto prefers auggie and reads
  only `auggie_model`, so the bare `model` would be silently overridden by auggie's default.
- **dry-run billing hint (audit F6)**: a subscription-first seat (cli:codex) whose fallback chain
  contains a billed channel now shows `⚠ fallback 含计费通道,降级时转计费` — the first-try
  billing verdict alone under-counted the downgrade cost.

### Fixed
- **`refine` had no abort gate (audit F2)**: an all-seats-failed refine round printed `done` and
  exited 0, so a scripted pipeline saw "refine happened" when it produced nothing. It now exits
  non-zero on zero output (aligned with `generate`'s abort) and marks partial rounds `[DEGRADED]`.
- **`early_stop_suggested` on incomplete evidence (audit F3)**: the refine early-stop signal fired
  on unanimous *surviving* seats even when other seats failed that round (survivorship bias). It
  is now suppressed whenever any seat failed the round (both review and decide branches).
- **Majority-verdict tie-break (audit F4)**: `_majority_verdict` returned the dict-insertion-first
  key on a tie, so a 2:2 split reported a spurious "majority" and mis-scored the sycophancy
  baseline. Ties now return `None` (no majority).

### Docs
- Disclosed three inherent limits surfaced by the audit: cross-seat prompt-injection propagation
  in refine/discuss rounds (F7), arbiter-same-family correlation in the consensus disclaimer
  (audit §7), and self-reported confidence as an ordinal-only signal + non-comparable
  novelty/feasibility scores across seats (synthesis judgment notes).
- README: platform support stated (Linux/macOS tested, Windows unverified — audit F8).

## [1.4.0] — 2026-07-13

> **Migration note**: bare `channel: cli` now means `cli_kind: auto` — when the `auggie` binary is
> on PATH it is preferred over codex (a one-time stderr banner announces this). In auto mode the
> auggie try does NOT inherit `cli_extra` (codex-specific flags) and takes its model only from
> `auggie_model`. **Opt-out / revert**: set `cli_kind: codex` on the member to get the exact
> pre-1.4.0 behavior. Also note auggie is a *billed* channel (upstream API price +40% via your
> Augment plan) — `dry-run` now counts it as `billed`, not subscription.

### Added
- **CH2 second CLI kind: auggie** (`cli_kind: auggie|codex|auto`, default auto → auggie preferred
  when detected; user decision 2026-07-13, benchmark: mem #10216). One auggie account serves all
  committee families (GPT/Gemini/Claude/Kimi/GLM). Hardening baked in: prompt via
  `--instruction-file` (not argv — ARG_MAX/injection, mirrors codex stdin), empty
  `--workspace-root` sandbox (prevents indexing the real project and codebase-context injection
  into blind review), `--output-format json` envelope (plain-text mode appends a `Request ID:`
  trailer that pollutes output), `--max-turns 1`, `--dont-save-session`, `--retry-timeout`
  (measured: concurrent Augment 503 retries hung >7 min without it; subprocess timeout remains
  the hard stop).
- **CLI-path JSON repair round**: cli seats (codex and auggie) now get one self-repair call on
  unparseable output before falling to the next channel, matching the api path (measured: 2 of 5
  benchmark runs emitted unescaped quotes inside JSON string values).
- `channel_used` now records the concrete kind (`cli:auggie` / `cli:codex`).
- Default committee (config.example.yaml): seats A (gpt5.6-sol) and C (gemini-3.1-pro-preview)
  move to auggie with codex/api fallbacks; 4th-family alternative comment switches from grok
  (needs a separate x-ai-supplied key, mostly list-only 404) to auggie `kimi-k2.7`
  (measured 41.8s/4.6KB).

### Fixed
- **Reasoning-model truncation on the api path** (the OpenRouter gemini 1KB-empty-shell bug):
  reasoning models (gemini-3.1-pro, gpt-5.6-sol) can burn the whole `max_tokens` budget on
  reasoning, returning an empty/truncated `content` with `finish_reason=length`. The old retry
  re-sent the identical request — deterministic re-failure. `call_model` now doubles the budget
  on each such retry (capped at 16000) and, if the final attempt still truncates with partial
  content, returns it best-effort for the parse/repair layer to salvage.

### Tests
- 137 → 162 (auggie channel: command shape / error classification / envelope + Request-ID
  fallback; cli_kind resolution incl. auto detection order; billing; cli repair round;
  truncation budget escalation ladder). Legacy cli tests pinned hermetic (`_which` stubbed).

## [1.3.3] — 2026-07-12

Fixes from a full second-pass audit of v1.3.2 (report: `docs/audit-report-2026-07-12-v1.3.2.md`).

### Fixed
- **Quorum denominator (N1)**: `generate`/`refine` computed the `min_successful_members` gate against *all* seats (including pure CH1 subagent seats), but only counted dispatchable (CH2/CH3) successes — so the default 2-subagent + 2-dispatchable committee would falsely `[abort]` "not enough advisors" when a single dispatchable seat failed, defeating the fallback/quorum degrade-and-continue path. The gate is now scoped to dispatchable seats; an all-CH1 config exits cleanly instead of aborting.
- `load_transcript` now skips corrupt `discussion.jsonl` lines (with a stderr count) instead of crashing the whole discussion on one bad line.
- `blindvote_<seat>.json` filenames now pass through `_safe_name` (closing the path-traversal gap left open on `seat`).

### Added (tests)
- `leak-check` exit-1 (secret found) regression test — previously only exit-0 (clean) and exit-2 (zero files scanned) were covered.
- `_dispatch_channels` fallback-chain traversal, `http_post` transport-layer request build, and `main()` argparse wiring smoke tests.
- Test count: 126 → 137.

### Docs
- Disclosed the line-level secret-scan false negative (real key on a line that also contains a placeholder word is skipped) in SKILL.md inherent-limitations.
- `discuss.md`: `--member` placeholder corrected to `<member_name>` (the filter matches on name, not seat letter).
- `roles-decide.md`: `is_fact` wording aligned to the generation schema's `facts[]`/`judgements[]` split.
- `config.example.yaml`: clarified that member `name` is a model-choice mnemonic, not the per-mode seat role.
- Corrected the quorum-gate phrasing in SKILL.md and both READMEs; added an English Troubleshooting table for parity with zh-CN.

## [1.3.2] — 2026-07-12
- English-first skill description; usage hint in the `/moa` menu.

## [1.3.1] — 2026-07-12
- Code-review fixes: `validate_config` now rejects member names that collide after filename normalization (`_safe_name`); corrected `dispatch_with_quorum` docstring overclaim; `bump-version.sh --check` validates `marketplace.json` by field-precise JSON parse instead of grep count.

## [1.3.0] — 2026-07-12
- Marketplace install: `.claude-plugin/marketplace.json` (`source: "./"`) + `plugin.json` `repository`; README dual install (marketplace primary / direct-copy fallback).
- `scripts/bump-version.sh`: single-source version sync across plugin.json → SKILL/README×2/marketplace, with a `--check` gate.

## [1.2.1] — 2026-07-12
- Documented four structural blind spots (prompt-injection non-immunity, `disputed` lower-bound, non-alignable anonymous labels, common blind spots) in a new SKILL.md "inherent limitations" section.

## [1.2.0] — 2026-07-12
- Code cleanup (dead-code removal, filename sanitize, `NO_PROXY=*` handling, refine/discuss sensitive-material warning, `billed_calls` rename); doc alignment; test coverage for `cmd_*` entry points and `call_cli_codex` branches.

## [1.1.7] — 2026-07-12
- P0 runtime fixes + P1 config hardening: min config schema validation; `refine`/`stats` no longer silently fall back to the example config; error-classification and doc-honesty fixes.

## [1.1.5] — 2026-07-12
- Free Opus subagents for the D and B seats (Self-MoA); fixed dry-run billing under-count for subagent seats with an api fallback.

## [1.1.0] — 2026-07-12
- Round-table discussion mode (L3): sequential turns, speaking-order rotation, conformity counting, pseudo-discussion detection, closing blind-vote drift check.

## [1.0.2] — 2026-07-12
- Fault-injection and disagreement chair-synthesis end-to-end tests; first stable release line.
