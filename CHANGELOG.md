# Changelog

All notable changes to the MoA skill. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project uses semantic-ish versioning (single source: `.claude-plugin/plugin.json`, synced by `scripts/bump-version.sh`).

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
