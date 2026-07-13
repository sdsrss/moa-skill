# Changelog

All notable changes to the MoA skill. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project uses semantic-ish versioning (single source: `.claude-plugin/plugin.json`, synced by `scripts/bump-version.sh`).

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
