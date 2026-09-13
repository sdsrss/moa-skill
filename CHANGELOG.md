# Changelog

All notable changes to the MoA skill. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project uses semantic-ish versioning (single source: `.claude-plugin/plugin.json`, synced by `scripts/bump-version.sh`).

## [1.8.0] — 2026-09-13

A QA pass driven by actually running the CLI rather than reading it — seven rounds over the documented
user paths (dry-run, generate, stats, refine, the discussion pipeline, leak-check, config handling),
stopping after two consecutive rounds found nothing above P3, plus what three rounds of independent
pre-ship review turned up. Eleven defects, each reproduced before it was fixed and each locked by a
regression test. Two of them produced **wrong committee readings** rather than crashes, which is the
failure mode this project exists to prevent.

> **Three previously-accepted invocations now fail fast.** All three used to "work" only in the sense
> of exiting 0 while producing something wrong or meaningless; none of them has a legitimate use.
> Following the v1.7.0 precedent for ISSUE-008, no opt-out flag ships — a flag would preserve the
> silent-failure mode being removed. To go back, pin the previous release.
>
> 1. **`stats` whose `--mode` disagrees with the artifacts in `--collect-dir`.** The error names the
>    mode it detected and prints the corrected command. **Action:** pass the `--mode` you generated with.
> 2. **`--round 0` or negative** on `refine` / `discuss-turn` / `discuss-prompt` (and negative on
>    `stats`; `stats --round 0` stays valid and means the generate round). **Action:** number rounds
>    from 1. For `refine` that is because round 0 *is* the generate round; for the `discuss` commands
>    it is simply the first speaking turn, and a non-positive round used to be written into
>    `discussion.jsonl` and shown to later speakers as "第 0 轮".
> 3. **A member whose `seat:` is present but empty *and* which sets no explicit `role:`.** An empty
>    seat resolved to the role key `""`, whose escaped regex matched an inline `## ` occurrence in
>    `roles-review.md`'s own prose — so that seat silently ran with the file's documentation preamble
>    as its role prompt. A member that sets `role:` is unaffected in `generate` / `refine`: `role`
>    wins over `seat` in role resolution, so its seat carries no role meaning there. The `discuss`
>    commands require a non-empty seat from every member regardless, because there `seat` is also the
>    anonymized speaker identity and the `blindvote_<seat>.json` filename.
>    **Action:** write `A`/`B`/`C`/`D`, or drop the key entirely to fall back to the default role.

### Fixed
- **A finished straggler no longer has its result thrown away at the grace-window boundary.**
  `dispatch_with_quorum` harvests completed futures, *then* registers grace deadlines, *then* expires
  them — so a seat that finished during the harvest callbacks was still in `pending` and got recorded
  as `skipped_grace` even though its result was already in hand. Both directions were wrong: a real
  HTTP 401 was laundered into "voluntarily skipped, not a fault", which zeroes out the
  `members_failed - members_skipped` difference `SKILL.md` teaches the arbiter to read, and discarded
  both the actionable hint and the already-billed `usage`; a *successful* straggler lost a paid-for
  committee opinion outright — in the reproduction, the only seat voting `fail` with a blocker, leaving
  `stats` reporting a unanimous `pass` with zero blockers. The expiry check now asks `fut.done()` first
  and takes the real result when there is one. Genuinely-running seats are still skipped exactly as before.
- **`stats` no longer emits an all-zero tally when `--mode` disagrees with the artifacts.** `--mode`
  defaults to `review` and artifacts do not record their mode, so `generate --mode brainstorm` followed
  by a bare `stats` silently produced `verdict_tally={'?':N}`, `mean_confidence=0.0` and zero issues —
  and `synthesis.md` requires the arbiter to copy stats numbers into the report verbatim, so that empty
  reading went straight into the deliverable. `stats` now infers the mode from the schema keys the
  successful seats actually carry and refuses to aggregate under the wrong one. The detector is
  deliberately conservative: every successful seat must point at exactly one other mode, and an
  ambiguous or unrecognizable shape stays silent. Failed seats are skipped rather than vetoing the
  check, so degraded runs — the common case here — are still covered.
- **`dry-run` no longer crashes on a config written the way the example config's comment tells you
  to.** `dict.get(k, default)` only substitutes when the key is *absent*; YAML `model:` (explicit
  null) returns `None`, which a `:<28` width format rejects with `TypeError`. A **primary** codex
  seat is written `model: null` — that is what `config.example.yaml`'s own comment instructs
  ("codex 兜底: model 必须置空") — and the very first step of the documented workflow, showing the
  user the roster and cost before spending anything, died on it. (The shipped file itself only uses
  `model: null` inside `fallback` entries, which `dry_run` never renders, so a stock config was not
  affected.) An explicit null in any roster column now renders as that column's empty placeholder
  rather than raising or printing the literal `None`.
- **One malformed field no longer destroys the whole refine-round aggregation.** ISSUE-002 hardened
  `compute_stats` and `compute_discuss_stats` against ill-typed member fields but skipped
  `compute_refine_stats`, where `verdict` and `revised_claimed_option` are used as dict keys and set
  elements. A model writing either as a list or an object raised `TypeError` at three sites and threw
  away every other seat's already-billed output. Non-string values are now excluded, and
  `early_stop_suggested` additionally requires every successful seat to have a *readable* stance —
  a stance nobody can read is not agreement.
- **`compute_discuss_stats` tolerates non-string seats.** `seat` doubles as a dict key and a sort key
  there, so mixing `seat: 1` and `seat: A` across members crashed `sorted()` on int-vs-str. Seats are
  normalized to strings for aggregation; string seats are unaffected.
- **The `[budget]` banner stops promising a fallback that does not exist.** It said
  "已让位给下一条 fallback" unconditionally, including when the exhausted link was the last (or only)
  one on the seat — sending the reader to debug a degradation chain that was never configured.
- **A straggler whose worker raises is recorded as a failure, not as a voluntary skip.** 1.7.1
  reached such a seat only through the expiry branch, which overwrote it with `skipped_grace` — the
  same laundering of a fault into "not a fault" as the headline fix above, and the same corruption
  of the `members_failed - members_skipped` difference. It is now recorded as a real failure
  carrying the exception text. Reachable because the worker runs `resolve_channel(member)` and
  `opts["timeout_seconds"]` *outside* `_dispatch_channels`' own `except Exception`.
- **Non-string `seat:` and `role:` values no longer crash role resolution.** `_seat_role` hands the
  raw value to `load_role_prompt`, which feeds it to `re.escape` — so `seat: 1`, `seat: false` or
  `role: 123` died with a raw `TypeError` naming the `re` module, and `seat: ["A"]` died even
  earlier on the unhashable `DEFAULT_SEAT_ROLE` lookup. Both sites now coerce to `str`, which is an
  identity map for every string seat and role — i.e. for everything the docs, the example config and
  `decide`-mode role injection produce — and lets an odd value fall through to the generic role
  sentence that already exists for unknown roles. (One exotic exception: a `custom_roles` key that is
  *not* a string, such as YAML's `1:` or `on:`, is no longer matched by an equally non-string
  `role:`/`seat:`; such a pair now lands on the generic sentence.)

### Changed
- **Named errors replace raw tracebacks when reading a user-supplied path.** ISSUE-005 established
  this for `--input` / `--inject`; the same doors elsewhere still raised library exceptions:
  - hand-written CH1 artifacts (`member_*.json`, `blindvote_*.json` — the `--collect-dir` seam where
    the *arbiter*, not the script, writes the file) gave `JSONDecodeError` without naming the file, or
    `KeyError: 'name'`;
  - a `config.yaml` with misaligned indentation or a tab gave the YAML scanner's error, and a
    `--config` pointing at a directory gave `IsADirectoryError` — while the same function already
    named the *missing*-config case;
  - an unwritable `--collect-dir` gave `PermissionError` from `mkdir` (the *creating* commands —
    `generate`, `discuss-turn`, `discuss-blindvote`; a write that fails later, e.g. `stats` writing
    into a directory that is read-only but already exists, still raises);
  - a non-UTF-8 file at any of those four doors gave `UnicodeDecodeError`. GBK-saved Chinese briefs and
    configs are a routine Windows artifact, and `leak_check` had handled this for its own reads all
    along. The message now names the encoding and gives the `iconv` invocation.
- **`seat` is validated at startup.** An empty `seat` used to surface either as `TypeError` from
  `re.escape(None)` deep inside role resolution — an error with no textual connection to the config —
  or, for `seat: ""`, as the silent first-section match described above. Only *empty* values are
  rejected, and only when the member sets no `role:`; `seat: 1` and a missing `seat` key still work
  (see the coercion fix above), because tightening further would be a breaking config change for no
  defect.

**Known, not fixed here.** Two raw-traceback paths remain, both confirmed to behave identically on
v1.7.1 and both deliberately left out to keep the repair round narrow:

- `options: {}` passes `validate_config` but then raises `KeyError: 'timeout_seconds'` at dispatch,
  because `_dispatch_channels` indexes `opts` directly while its siblings
  (`min_successful_members`, `grace_seconds`) use `.get()` with a default. Write the `options` block
  as `assets/config.example.yaml` does.
- `--models` combined with a config that parses to `None` (a zero-byte `config.yaml`, or
  `--config /dev/null`) raises `TypeError: 'NoneType' object is not iterable`, because
  `apply_custom_committee` runs before `validate_config`. Without `--models` the same input gives the
  named `[config] 顶层必须是 YAML 映射(dict)` error.

tests 231 → 305.

## [1.7.1] — 2026-09-13

Same-day patch for three regressions v1.7.0's own repairs introduced. A pre-ship reviewer's verdict
landed after the tag went out; these are the items it scoped as in-release, and all three are
defects created by v1.7.0 rather than pre-existing ones. **Upgrade from 1.7.0 is recommended for
anyone whose `config.yaml` has a `cli` seat**, which the first item can stop from starting at all.

### Fixed
- **A `cli` seat whose fallback omits `channel:` no longer fails validation.** Omitting `channel:`
  in a fallback entry is legal and means `api` — that is exactly how `resolve_channel` dispatches
  (`fb.get("channel", "api")`). v1.7.0's new fallback gate judged the merged `{**member, **fb}`
  view, which inherits the *member's* `channel`, so a `cli` seat carrying a plain
  `{model: …, protocol: openrouter}` fallback was rejected at startup with an error that called
  that link `channel=cli`. The gate now pins `channel` to the fallback's own value and judges the
  remaining keys (`cli_kind` / `model` / `auggie_model`) on the merged view, which is what
  `_expand_cli` actually reads. Genuinely ambiguous fallbacks — an explicit `channel: cli` with no
  `cli_kind` and no `auggie_model` — are still rejected.
- **`--retry-timeout` is integral again.** v1.7.0 started handing the cli repair round the
  remaining link budget as a float, and the flag is built with `str(max(30, timeout // 3))`, so
  auggie received `73.0` where it had always received `73`. If its parser is strict that turns the
  one-shot JSON self-repair into a guaranteed non-zero exit on every auggie seat — three of the four
  in the shipped committee. Now `int()`-wrapped at the point the argument is built, so it holds
  whatever the caller passes.
- **The fast exit now stands down while a CLI call is in flight.** v1.7.0 reasoned that `os._exit`
  only skipped `TemporaryDirectory`'s cleanup and deleted the registered directories to compensate.
  It also kills `subprocess.run`'s own timeout watchdog: an abandoned `auggie`/`codex` child is
  reparented and runs unbounded (this project has measured auggie's internal retry at over seven
  minutes, billed at upstream +40%), and deleting the directory pulled its workspace out from under
  it. When any CLI call is still running the process now takes the regular shutdown instead — the
  pre-1.7.0 behavior, so nothing regresses; it simply forgoes the speed-up in that one case, and the
  watchdog, the context manager and normal child reaping all work again. The directory deletion
  described in 1.7.0's ISSUE-009 entry is gone with it.

tests 228 → 231.

## [1.7.0] — 2026-09-13

Degradation-resilience pass over the three-channel fallback path, driven by three instrumented
probes (kept in the gitignored `tasks/probes/`, re-runnable, no real requests). The probes showed
that channel *switching* was immediate but *failure detection* was effectively unbounded, and that
one of the two channel branches did not degrade at all.

> **Upgrading from 1.6.x — read this.** Two defaults change, and both can alter what a run does.
>
> 1. **`timeout_seconds` now means "per fallback link", not "per HTTP attempt".** A seat that used
>    to succeed only by burning two or three retries past its timeout can now be cut at the budget
>    boundary and handed to the next fallback instead. **Action:** if a seat legitimately needs
>    retry headroom, raise that seat's `timeout_seconds` — a seat's worst case is now
>    `expanded links × timeout_seconds`, so the value is readable as a bound. Count *expanded*
>    tries: a `cli` seat with no explicit `cli_kind` becomes two links (auggie, then codex) when
>    both binaries are present. Every `cli` seat in the shipped config sets `cli_kind`, so there the
>    two counts agree. Nothing to change if
>    your seats normally answer on the first call. The first time a link is actually cut, the run
>    prints a one-time `[budget]` note on stderr explaining this and naming the seat, so you do not
>    have to have read this entry.
> 2. **A `cli` seat with `model:` but no `cli_kind:` and no `auggie_model:` is now rejected at
>    startup** instead of silently running auggie's default model. **Action:** the error names both
>    fixes — add `auggie_model: <auggie-side id>`, or set `cli_kind: codex`/`auggie` explicitly.
>    Every `cli` seat in the shipped `config.example.yaml` already sets `cli_kind`, so a stock
>    config is unaffected.
>
> **Revert path:** no runtime opt-out flag ships for either change — both are corrections to what
> the existing knob and the existing config already claimed to mean, and a flag would preserve the
> silent-failure mode they remove. To go back, pin the previous release:
> `/plugin install moa@moa-skill` after `/plugin marketplace add sdsrss/moa-skill#v1.6.2`, or for a
> direct-copy install `git checkout v1.6.2 -- skills/moa`.

### Changed
- **`timeout_seconds` now bounds a whole fallback link, not a single HTTP attempt.** It was applied
  per attempt and handed unchanged to every link, so `call_model`'s two internal retries multiplied
  it: a probe with three `api` links at `timeout_seconds: 240` issued **9 HTTP attempts and could
  run 2169 s (~36 min) for one seat**, and the quorum grace window did not help because it only
  opens *after* quorum is reached — when two of three seats fail, it never opens at all, so the
  bound was missing exactly when degradation mattered. Each link now gets its own wall-clock budget
  covering its retries, backoff and JSON repair round; a link that exhausts it fails with
  `err_class: budget` and yields to the next one. A seat's worst case is therefore `expanded links ×
  timeout_seconds` — expanded, because a `cli` seat with no explicit `cli_kind` becomes two tries
  (auggie, then codex) when both binaries are present; the shipped config sets `cli_kind` on every
  `cli` seat, so there the config count and the expanded count agree. The budget is allocated
  **per link rather than shared across the seat**, so
  every fallback the user configured is still tried at least once. What is tightened is wall clock,
  not retry policy: fast failures (a 429 returned immediately) consume almost no budget and still
  get the full retry count, and the common "one call, succeeds" path is unchanged. No new config
  option — this makes the existing knob mean what it reads like. (ISSUE-007)
- **A `cli` seat that would run an unknown model is now rejected at config validation instead of
  warned about.** `channel: cli` without an explicit `cli_kind` resolves to auggie when the binary
  is present, and that path only honours `auggie_model` — so a member carrying `model:` but no
  `auggie_model:` silently ran auggie's *default* model and recorded `model_used: null`. The cost is
  not merely "config ignored": `references/synthesis.md` requires the arbiter to disclose each
  seat's model family so readers can discount an "everyone agrees" result, and a seat of unknown
  family makes that hard rule unexecutable — cross-family de-correlation is the committee's whole
  premise, so silently running an unknown model is a correctness failure, not an acceptable
  degradation. The error names both fixes (add `auggie_model`, or set `cli_kind` explicitly). Every
  `cli` seat in the shipped `config.example.yaml` sets `cli_kind`, so this gate cannot fire on a
  stock config; a test now asserts that. (ISSUE-008)

### Fixed
- **`generate` / `refine` no longer hang after they are done.** Stragglers abandoned by the grace
  window keep running in background threads, and `concurrent.futures`' atexit handler joins them
  at interpreter shutdown — a probe measured the dispatcher returning at 0.55 s while the process
  only exited at 6.1 s, which at the default `timeout_seconds: 240` means up to 4 minutes of
  silence after `done` is printed. That lands precisely where `SKILL.md` step 3 tells the arbiter to
  background `generate` and dispatch CH1 seats in parallel, and it stalls `generate && stats`
  chains. `main()` now exits the process directly once a straggler has been abandoned. Artifacts are
  safe: every `write_member` happens on the main thread's `on_done` callback, so abandoned threads
  never write files. Before exiting it also deletes the CLI channels' temp directories, which the
  abandoned thread is parked inside and which `os._exit` would otherwise leave behind — the auggie
  channel writes the whole briefing to `prompt.txt` in there, so skipping the cleanup would strand
  review material in the system temp dir. Only the clean path does this — a non-zero `sys.exit`
  still takes the regular (slower) shutdown, where the user is reading an error anyway. (ISSUE-009)
- **`api` seats now degrade on unparseable output, like `cli` seats already did.** The two channel
  branches of `_dispatch_channels` handled the same failure asymmetrically: when a member's output
  could not be parsed as JSON even after the repair round, the `cli` branch raised and the seat fell
  through to the next link in its `fallback` chain, while the `api` branch returned a `parsed=None`
  result immediately — consuming the seat and **voiding every remaining fallback link**, so an `api`
  seat with a configured degradation chain behaved as if it had none. The failure also recorded
  `err_class: null`, making this class invisible to the error tally in `stats.json`, and dropped the
  `usage` of the two calls (generate + repair) that had already been billed, so cost reporting
  under-counted exactly when things were going wrong. The `api` branch now records a `parse`-class
  failure carrying `usage` and `raw`, then continues down the chain. `_fail` takes optional
  `usage` / `raw` arguments; all other call sites keep their previous shape. (ISSUE-006)

  Member artifacts for failed seats now always carry a `usage` key (`null` when nothing was billed).
  This holds even when the repair round itself raises — the generate round's already-billed `usage`
  and its `raw` ride out on the exception rather than dying with the stack frame, which matters
  because the per-link budget below makes "generate ate the budget, repair cannot run" a routine
  path rather than a rare one.
- **A failed seat's artifact now names the link that actually ran.** `_fail` built `model_used` and
  `protocol` from the member's *primary* channel while the `raw` and `usage` it carried came from
  whichever fallback link produced them, so one link's bytes shipped under another link's identity —
  and `roster[].model_known` would report the config's `model` as *known* for a seat that never ran
  it, defeating the family-composition check `references/synthesis.md` asks the arbiter to perform.
  Failure records inside the fallback loop now carry the real link's `model_used` / `protocol` /
  `channel_used`; the last of those also restores a field v1.6.2 populated and the first cut of this
  release had regressed to `null`.

### Added
- **`members_skipped` separates abandonment from failure.** A seat dropped when the grace window
  expired is a deliberate "we are not waiting for it", not a fault, but it counts in
  `members_failed` all the same. `members_failed` keeps its old meaning (every non-successful seat)
  so existing readings do not shift; `members_skipped` is a subset of it, and real failures are
  `members_failed - members_skipped` without reading `err_class` seat by seat. (ISSUE-011)
- **`roster[].model_known`** flags seats running a channel default model — `cli_kind: codex` with
  `model: null`, the shipped fallback idiom, is one — whose family cannot be determined.
  `synthesis.md` now tells the arbiter to exclude those seats from family counts and name them in
  the report rather than guessing from the config. (ISSUE-008 companion)
- **`dry-run` states that its call count is a lower bound**, naming what it excludes: JSON repair
  rounds, transient retries (with their `max_tokens` doubling), discussion rounds, and subscription
  seats that turn billable on degradation.

### Deliberately not shipped
- **Aggregate reporting of what failed seats burned.** This release grew `token_usage.wasted_tokens`
  / `wasted_members` and then withdrew them before release, because pre-ship review showed the pair
  wrong in both directions at once. Under-count: the largest single token sink in the file is
  `call_model`'s truncation retry, which doubles `max_tokens` each attempt — measured at
  3000 → 6000 → 12000, i.e. 21000 tokens genuinely billed across three HTTP 200s — and it discards
  each attempt's `usage` inside its own retry loop, so the figure never reaches the aggregator and
  reports `0`. Over-count: when a provider omits the usage block, `_merge_usage({})` yields an
  all-zero but *truthy* dict, so a seat that was never billed is counted as a wasted member.
  A cost field that exists to make hidden spend visible, and is wrong in both directions, is worse
  than no field — this CHANGELOG would have been telling you to read it. The root cause is
  structural (usage rides local variables along the normal return path, so every exception path is
  a drop site) and patching the sites one at a time does not converge; the fix is a per-seat
  accumulator that records spend independently of how the stack unwinds, and it gets its own
  release. Per-seat `usage` in `member_*.json` is still preserved on a best-effort basis and makes
  no completeness claim.

## [1.6.2] — 2026-07-13

Robustness hardening from an autonomous QA self-test loop (5 rounds, black-box + white-box).
All changes are bugfixes / fail-fast on already-broken input; **no behavior change for valid
configs or well-formed member output** (well-formed stats output verified byte-identical).

### Fixed
- **Aggregation no longer crashes on ill-shaped member output.** `parse_json` returned any
  parseable JSON (a top-level array/scalar/bool), which is truthy and was treated as a successful
  member — then `compute_stats` / `compute_refine_stats` / `compute_discuss_stats` /
  `_majority_verdict` called `.get()` on it and raised `AttributeError`, killing the whole `stats`
  run and every other paid seat's tally. `parse_json` now returns a dict-or-`None` (recovering an
  embedded `{...}` from a single-object array like `[{...}]`), and the aggregation layer independently
  gates success on `isinstance(parsed, dict)` so the CH1 arbiter-hand-written `member_*.json` path
  (which bypasses `parse_json`) can't crash `stats` either. (ISSUE-001)
- **Nested member fields are now type-guarded.** Object-array fields (`issues`, `opponent_fatal_flaws`,
  `ideas`, `cross_exam`, `verdicts_on_others`, `responses`) were iterated as list-of-dict and
  `.get()`'d, and `confidence` was summed as a number — so a single seat returning `issues: "none"`,
  `confidence: "high"`, `verdict: ["pass"]`, or `ideas: ["a","b"]` crashed the whole aggregation.
  Added `_dict_items` / `_num` / `_str` guards and coerced tally keys (verdict / severity /
  claimed_option) to strings (avoids unhashable-key crashes). Malformed fields are tolerated
  field-by-field, not fatal. (ISSUE-002)
- **The remaining numeric config options are validated** like `grace_seconds` was in v1.6.1:
  `min_successful_members` (non-negative; `0` = no floor), `timeout_seconds` (global + per-seat),
  and `max_tokens_member` must be numbers. A quoted `min_successful_members: "2"` previously raised
  an uncaught `TypeError` aborting the whole run; quoted `timeout_seconds` / `max_tokens_member`
  produced cryptic `float + str` seat failures. All now exit with a named `[config] …` error. (ISSUE-003)
- **`discuss` now requires unique seats.** In `discuss`, `seat` is the anonymized speaker identity —
  it drives how members reference each other (`responses.to` "委员X"), the transcript speaker labels,
  and every stats/blindvote aggregation key (`last_by_seat`, `bv_by_seat`, `blindvote_<seat>.json`).
  Two members sharing a seat silently broke all three (ambiguous cross-references, indistinguishable
  labels, one seat dropped from drift/dissent via overwrite). `discuss-turn` / `discuss-prompt` /
  `discuss-blindvote` now fail fast with a named error naming the colliding members; `discuss-blindvote`
  additionally refuses to overwrite a seat's vote already written by a different-named member
  (same name = idempotent re-run). Duplicate seats remain **legal in `generate` / `refine`** (keyed by
  unique `name`, e.g. two feasibility skeptics) — the guard is scoped to `discuss` only. (ISSUE-004)
- **Missing `--input` / `--inject` files give a named error** instead of a raw `FileNotFoundError`
  traceback. `generate` / `refine` / `dry-run` / `discuss-*` read the file with no existence check,
  so a typo'd brief path — the most common argument — dumped a traceback while every other missing
  resource (config, products) got a clean message. Added `_read_input` / `_read_inject` with
  `[input]` / `[inject]`-prefixed errors. (ISSUE-005)

### Testing
- Suite 177 → **209** (+32 assertions): non-object `parse_json` recovery, non-object/malformed-field
  tolerance across all three stats modes + discuss, the four new numeric-config rejections (global +
  per-seat), missing-input/inject named errors, and the `discuss` seat-uniqueness gate + blindvote
  overwrite guard. `leak-check` clean.

## [1.6.1] — 2026-07-13

Hardening follow-up to v1.6.0's new per-seat `grace_seconds` field (from a fresh code review).
No behavior change for valid configs; strictly fails faster on already-broken input.

### Fixed
- **`grace_seconds` now validated in `validate_config`** (global `options.grace_seconds` and
  per-seat `member.grace_seconds`): must be a non-negative number. Previously a quoted/non-numeric
  YAML value (`grace_seconds: "90"`) raised an opaque `TypeError` deep inside the thread dispatch
  and aborted the whole run, and a negative value (`grace_seconds: -5`, a plausible typo) made the
  window expire immediately — silently dropping the very seat it was meant to keep. Both now exit
  at config-validation time with a named `[config] …` error, consistent with the existing
  channel / cli_kind / duplicate-name fail-fasts. `bool` is rejected too (int subclass, not a
  seconds value). Absent field is unchanged (uses the default).

### Testing
- Added 5 negative cases (global & per-seat × non-numeric / negative / bool) to
  `test_validate_config_rejects_broken`, a positive `test_validate_config_accepts_valid_grace`
  (int / float / 0 / absent), and `test_dispatch_member_grace_zero_skips_immediately_under_large_global`
  (per-seat window overrides a large global window). Suite 170 → 177.

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
