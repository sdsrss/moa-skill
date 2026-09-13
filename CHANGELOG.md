# Changelog

All notable changes to the MoA skill. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project uses semantic-ish versioning (single source: `.claude-plugin/plugin.json`, synced by `scripts/bump-version.sh`).

## [1.11.0] — 2026-09-13

### Upgrading — `dry-run` now makes one network call

`dry-run` used to touch the network zero times. It now issues a single free, unauthenticated
`GET https://openrouter.ai/api/v1/models` to check your model slugs. Nothing else about the run
changed: no new spend, no new failure mode, same exit codes. (The one cosmetic difference: building
the HTTP opener makes the pre-existing `[proxy] detected env proxy` line appear on stderr, where
`dry-run` previously never built one.)

- **What you must do**: nothing, if that call is fine in your environment.
- **If it is not** — air-gapped, egress-filtered, or you simply do not want the call — pass
  `--no-model-check`. That is the complete opt-out; the rest of `dry-run` is unaffected.
- **What you will see if the call cannot complete**: one line saying the check was skipped, and the
  dry run proceeds. A refused connection returns in milliseconds; a blackholed one burns the 5s
  socket timeout. That 5s does **not** bound DNS: `socket.getaddrinfo` is outside urllib's timeout,
  so a resolver that silently drops the query adds the system resolver's own timeout on top — 20s on
  one machine whose resolver could not be reached, where the same command on 1.10.0 took 0.1s. That
  figure is the operating system's resolver timeout, not something this code sets, so treat it as an
  example rather than a bound. If your egress filters DNS rather than refusing connections, use
  `--no-model-check`.
- **Programmatic callers**: `dry_run()` itself defaults to `model_check=False`, so importing and
  calling it never reaches the network. Only the CLI opts in.

### Added
- **`dry-run` now checks your model slugs against OpenRouter's live list.** The README has told
  people "model IDs churn fast; verify once with `dry-run`" since the beginning, and `dry-run`
  verified nothing — it printed whatever slug the config contained. It now fetches
  `GET /api/v1/models` (free, and **no** `Authorization` header: the endpoint does not need a key,
  so none is sent) and marks each **comparable** api link `OK` / `UNKNOWN` / `MISSING`. A retired or mistyped
  slug is visible before the run starts, instead of surfacing as a 400 on one seat after the other
  seats have already been billed.

  Links it cannot honestly compare are marked `skip`, never guessed at: cli and subagent seats do
  not use OpenRouter slugs at all, and a custom `base_url` or a non-`openrouter` `protocol` has its
  own model namespace — judging those against OpenRouter's list would be all false alarms, which is
  worse than not checking.

  Which links those are is decided exactly the way `resolve_channel` decides it: the main link by the
  member's own `channel`, a fallback by **the fallback's own** `channel` (absent means api), never by
  the value a fallback inherits from its member. Getting that backwards is what v1.7.0's A1 did — it
  judged the channel off the merged view and refused legal configs, and v1.7.1 shipped the same day to
  undo it. Here the same mistake fails the other way: a cli seat whose fallback omits `channel` really
  does run as an api link, and judging it off the merge would silently mark it `skip` — exactly the
  case this feature exists for, since that link sends the member's auggie-side model name to
  OpenRouter. The merged view is still what supplies the link's model, protocol and `base_url`.

  The check is **fail-soft**: offline, blocked at the egress, endpoint moved or reshaped, it prints
  one line and the dry run continues with its exit code unchanged — that now includes the judging
  step, not just the fetch, so a config field of an unexpected type degrades the check instead of
  taking down the dry run. The socket timeout is 5s (a refused connection fails in milliseconds, a
  blackholed one burns it); DNS resolution is not covered by it. `dry-run` is the command `SKILL.md`
  has the arbiter run in front of the user, which is why the bound matters. `--no-model-check` turns it off entirely, and the
  `dry_run()` function defaults to *off* so that importing it never reaches the network — the
  decision to make that GET belongs to the CLI boundary, and the test suite stays offline.

### Fixed
- **An api link with no `model` now says so.** `call_model` indexed `cfg["model"]` directly, so the
  seat died on a `KeyError` that `_dispatch_channels` caught and reported verbatim: the user's
  `error` field read `'model' [unknown]`, which names neither the seat's problem nor its fix, and
  the `unknown` class polluted the error table `SKILL.md` teaches the arbiter to read. It now raises
  the same `PermanentError` shape the cli branch uses when its binary is missing from `PATH`
  (`err_class=startup`, with a hint), because both are the same thing: a link whose precondition is
  absent, which cannot be attempted at all.

  **No new rejection.** `validate_config` still accepts an api member without a model — that is
  pinned by `test_validate_config_accepts_valid`, and adding the gate a reviewer proposed breaks 22
  tests, 4 of them `accepts_*` contract tests. A single-model OpenAI-compatible gateway behind
  `base_url` is also a legitimate reason to omit it. The link still falls through to the next
  fallback exactly as before; only the message and the class changed. Rationale is recorded in
  `tasks/specs/model-preflight.md` so the gate is not proposed a third time.

Tests 372 → 427.

## [1.10.0] — 2026-09-13

### Upgrading — only if your `options:` block is not complete

If every one of `timeout_seconds` / `max_tokens_member` / `min_successful_members` /
`grace_seconds` is set in your config (the shipped `assets/config.example.yaml` sets all four),
**nothing changes for you** and you can stop reading: `dry-run` output is byte-identical to 1.9.0.

If any of them is missing, or is written with an empty value, this release changes what happens.
Before, such a config was accepted by validation and then died at dispatch with a raw traceback.
Now the missing keys take documented defaults and **the run proceeds and bills** — `max_tokens_member`
is a per-call spend ceiling and `timeout_seconds` is a per-fallback-link wall clock, so a config that
previously could not start now can spend money.

- **What you must do**: nothing, if the defaults suit you. To keep full control, write the four keys
  explicitly into your `options:` block, copying `assets/config.example.yaml`.
- **How you will notice**: the script prints one `[options]` line to stderr naming every key it
  defaulted and the value it used. It appears once per run, and never for a complete config.
- **How to revert**: pin 1.9.0. There is no flag to restore the old behaviour, because the old
  behaviour was a crash.

### Fixed
- **A half-written `options:` block passed validation and then crashed at dispatch.** Both numeric
  validators in `validate_config` state in their own comments that an unset key is legal because it
  "uses the default" — and four accept-tests pin that contract (27 test sites pass `options: {}` as
  filler). No default layer ever existed. The same contract therefore broke differently on each
  half of the key set, and the two halves differ in how much they cost you:

  - `timeout_seconds` and `max_tokens_member` were read with a bare subscript. A **missing** key
    raised `KeyError: 'timeout_seconds'` at the first dispatch — loud, immediate, before any
    request went out. A key **written blank** was worse in a quieter way: the subscript found it and
    returned `None`, which travelled into the socket timeout and into the `max_tokens` payload and
    the truncation-retry arithmetic.
  - `grace_seconds` and `min_successful_members` were read with `.get(key, literal)`, which falls
    back only when the key is **absent**; a key written blank is an explicit `None` and went
    straight through. `min_successful_members:` then died in `cmd_generate`'s `min(None, …)` before
    dispatch, costing nothing. `grace_seconds:` is the expensive one: the deadline it feeds is only
    computed once quorum is reached *and* stragglers are still pending, so the same config can work
    one day and, on a slower day, die at `time.monotonic() + None` after several seats have already
    answered and been billed, with part of the artifact set already on disk.

  There is now one source: `DEFAULT_OPTIONS` plus an `_opt()` reader used at all six call sites.
  Missing keys and blank keys are treated alike. The test is `is None` rather than `or` — not a
  behaviour repair (the old `.get(key, literal)` also passed an explicit `0` through) but a guard
  against writing the obvious thing later: `grace_seconds: 0` (wait for nobody) and
  `min_successful_members: 0` (no floor) are both values the validator explicitly admits, and `or`
  would silently promote them to 30 and 2.

  The defaults are the shipped example's values — with one deliberate exception. `grace_seconds`
  stays at **30**, not the example's 90; that split is the documented v1.6.0 back-compat contract,
  recorded in `SKILL.md` and in `config.example.yaml`'s own comment. A test now reads the shipped
  example and asserts the three tracking keys match it exactly and that `grace_seconds` is the
  documented 30-vs-90 exception, so neither side can drift into the other.

  This closes both of the `Known, not fixed here` items recorded under 1.8.0 below.

- **`--models` bypassed the named config error and raised a raw `TypeError`.** `main()` runs
  `resolve_config` → `apply_custom_committee` → `validate_config`, so `--models` reaches
  `dict(cfg)` before anything has checked the config's shape. An empty YAML file parses to `None`,
  giving `TypeError: 'NoneType' object is not iterable` — while the *same* config without
  `--models` produced `validate_config`'s named `[config]` message. One door, two treatments; the
  same asymmetry ISSUE-005 was opened for. `apply_custom_committee` now returns a non-mapping
  config untouched so the existing named error is the one users see on both paths. No second
  message was added, and the tests assert both paths produce that same sentence.

### Added
- **One `[options]` line on stderr when a key was defaulted.** It names each unset key, the value
  substituted, what that value governs (spend ceiling / wall clock / abort floor / grace window),
  how to pin it, and that 1.9.0 and earlier would have crashed here instead of calling out. It is
  emitted once per run from `main()`, after validation and before any dispatch, so it covers
  `generate` / `refine` / `discuss-*` / `dry-run` in one place and cannot interleave with worker
  threads. A complete config never triggers it.

Tests 355 → 372. Coverage was measured by mutation rather than asserted: 19 reverts of the changed
lines, 18 caught by the suite. The single survivor is the `(opts or {})` guard inside `_opt`, which
`validate_config` makes unreachable — defensive, not a tested path. An independent empty-context
pre-ship review (`tasks/preship-review-unreleased.md`) is what found the first version of this net:
it missed the two `cmd_refine` call sites entirely, so both could be reverted line-for-line with the
suite still green. Those two tests are now here, as is a gate tying `DEFAULT_OPTIONS` to the shipped
example.

## [1.9.0] — 2026-09-13

Closes ISSUE-012 — the per-seat usage ledger that `tasks/specs/degradation-budget.md` has been
carrying as "the correct way to do what 1.7.0 withdrew". Mostly additive: new fields, no new
rejection, and two deliberate narrowings of existing readings (`billed_members` and
`billed_calls`, below).

### Added
- **`token_usage.wasted_tokens` / `wasted_members` are back, and this time they are right.** 1.7.0
  shipped them and withdrew them the same day because pre-ship review showed they were wrong in
  *both* directions at once: the single largest consumer — a reasoning model's truncation retries,
  billing `max_tokens` 3000 → 6000 → 12000 for 21,300 tokens — lost its `usage` inside
  `call_model`'s retry loop and reported **0**; and a provider that omits `usage` produced an
  all-zero but *truthy* dict, counting a seat that spent nothing as `wasted_members: 1`.

  The root cause was neither field. `usage` travelled in local variables along the *successful
  return path*, which makes every exception path a discard point — and patching them one at a time
  does not converge (round 1 patched `call_with_json_repair`; round 3 found the same hole one level
  down in `call_model`). It is now a **ledger object**: every billed response is recorded the moment
  it arrives, so however the stack unwinds, the entry is already made. Callers read it once at the
  end instead of threading a value back up.

  Recording early is necessary but not sufficient — pre-ship review caught the follow-on: the ledger
  lives in the worker thread's frame, while a seat abandoned at the grace window is judged in the
  *main* thread, which could not reach it. The stack had not unwound; the ledger was alive and simply
  unreachable, so an 18,000-token seat was reported as zero waste. Each seat's ledger is now published
  for the abandonment path to read — but only what has *already landed* by then. A single long call
  still in flight when the window expires is still not counted, which is why the figure below is a
  lower bound rather than a total.

  `wasted_tokens` covers both a fully failed seat's entire spend and the failed links of a seat that
  succeeded after degrading, and it is computed **per seat and clamped per seat** so that one
  artifact with an inconsistent shape cannot cancel out another seat's real waste. It is **not**
  folded into `total_tokens`, which keeps its existing meaning — "what the opinions cost" — because
  the README's cost multiple is read against it. `wasted_members` counts only seats that were billed
  and produced nothing; a seat that degraded and then succeeded is not one, though its burned links
  are in `wasted_tokens`. Subscription seats (CH1, CH2 codex) have an empty ledger and appear in
  neither. Both fields are reported for refine rounds too (`stats.r<N>.json`).

  **Report it as a lower bound.** True spend is **at least** `total_tokens + wasted_tokens`. A seat
  abandoned at the `grace_seconds` boundary keeps running in the background and keeps billing; its
  ledger is read at the moment of abandonment, so anything it spends afterwards is not counted. An
  artifact written by an older version has no ledger at all and contributes only what it
  self-reports. A hand-written CH1 artifact contributes nothing even if it self-reports tokens —
  CH1 runs on a subscription, so those tokens are not money, and counting them would push the
  figure in the *other* direction.
- **Per-seat artifacts gain `usage_total`** — everything that seat was billed, across every fallback
  link and retry — alongside the existing `usage`, which still records only the link that produced
  the opinion. Present on every per-seat artifact this version writes — member files, seats skipped at the
  grace window, `--inject`ed CH1 seats, discussion-turn envelopes and `blindvote_<seat>.json` (the
  closing blind vote is itself a billed call and carries its own ledger; `--inject`ed seats are
  always zero, because CH1 runs on a subscription).

### Fixed
- **`billed_members` no longer counts seats that were never billed.** It filtered on the truthiness
  of the `usage` dict; an all-zero dict from a provider that omits usage is truthy. It now filters on
  the token count — *any* of `prompt_tokens` / `completion_tokens` / `total_tokens` being positive,
  the same rule the ledger uses. Deliberately not `total_tokens` alone: `base_url` accepts any
  OpenAI-compatible endpoint (vLLM, LiteLLM, a local gateway), and a response carrying only
  prompt/completion counts would otherwise be judged "never billed" and have its real spend dropped
  from the aggregate along with it.

  `billed_calls` in `discuss_stats.json` had the identical defect and gets the identical fix, so
  **two** existing readings change meaning in this release, both in the same direction: an all-zero
  usage dict no longer counts as a billed call or a billed member.
- **Four `raise` statements inside `except` blocks now chain with `from`**, so the underlying
  `HTTPError` / `TimeoutExpired` / `ValueError` survives into the traceback instead of being replaced
  by the translated error. Found by running `ruff --select B904` over the tree; see the note below on
  why no lint gate ships with it.

### Note on linting
A lint gate was evaluated for CI and **deliberately not added**.

Measured with `ruff 0.14.2 --isolated --no-cache --select E4,E7,E9,F` (the classic default set) over
`skills/`, at the point v1.8.0 was tagged. It reported 6 findings in `moa.py`: one placeholder-free
f-string and five deliberate semicolons. Widening to `B904` added 4 more. Those two rules are the
only ones that carried signal, and both are fixed above, so they now report zero.

Everything else the wider rule set surfaces is house style: `E501` line-too-long and `RUF00x`
ambiguous-unicode, in the several hundreds and the couple of dozen respectively. No exact count is
quoted here on purpose — both grow with every Chinese comment, so any number written down goes stale
by the next commit, and the decision does not turn on the figure. `RUF00x` is firing on the `×` in
prose like `链数 × timeout`, where it is correct.

More to the point: **none of the eleven defects the v1.8.0 QA pass found would have been caught by
it.** They were type, semantic and concurrency defects — a `None` reaching a format spec, an
unhashable dict key, an uncaught decode error, a grace-window race, aggregation under the wrong mode.
A gate here would fail on house style while staying silent on the class of defect that actually bites
this project.

tests 305 → 355.

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
