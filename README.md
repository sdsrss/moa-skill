# MoA Skill — a Multi-Model Committee for Claude Code

**English** · [中文](README.zh-CN.md)

> A [Claude Code](https://docs.claude.com/en/docs/claude-code) skill that turns your main agent into the **chair of a five-model committee**. Up to 4 heterogeneous LLM members review, decide, or brainstorm in parallel — **independent blind generation → structured interaction → evidence-driven convergence** — producing conclusions more reliable than any single model.

Built on **Mixture-of-Agents (MoA)**: different models have different blind spots, so independent blind review plus structured aggregation beats a single model on judgment-heavy tasks. Positive gains apply to **LLM-judge–style subjective work only** — don't use it for simple Q&A or mechanically-verifiable objective problems (arithmetic, fact lookup).

<p>
<img alt="status" src="https://img.shields.io/badge/status-v1.3.0-brightgreen"> <img alt="tests" src="https://img.shields.io/badge/tests-125%20passing-brightgreen"> <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-blue"> <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

## Table of Contents

- [Why MoA Skill](#why-moa-skill)
- [Installation](#installation)
- [Features](#features)
- [Highlights](#highlights)
- [How It Compares](#how-it-compares)
- [Usage](#usage)
- [Cost](#cost)
- [FAQ](#faq)
- [Development](#development)
- [License](#license)

---

## Why MoA Skill

One model reviewing its own work is one set of blind spots. MoA Skill convenes **up to four independent, heterogeneous models** across the OpenAI / Anthropic / Google / xAI families that never see each other's raw output during generation, then lets **your current agent — holding full context — arbitrate** under hard anti-groupthink rules. You get a second, third, fourth opinion that is *actually independent*, with disagreements surfaced rather than averaged away.

> **On the default roster:** the shipped `config.example.yaml` fields three families — OpenAI (codex) + Anthropic (Opus) + Google (Gemini) — plus a fourth **role-differentiated Self-MoA seat** (a second Anthropic model in an adversarial role). The fourth *family*, xAI/Grok, is opt-in: it needs an OpenRouter key with x-ai supply (many keys return list-only 404 for Grok), so swap it in per the caveats in [`config.example.yaml`](skills/moa/assets/config.example.yaml). Family count is a config choice, not a hard-coded four.

It is a **committee you summon on demand — not an auto-development pipeline.** Coding and fixing stay with your main agent; MoA is for the judgment-dense nodes: review, decision, arbitration, brainstorming.

---

## Installation

Two ways to install. **Marketplace** (added in v1.3.0) is the one-liner; **direct-copy** is the dependency-free fallback.

```bash
# Option A — Claude Code marketplace (recommended)
/plugin marketplace add sdsrss/moa-skill
/plugin install moa@moa-skill

# Option B — direct-copy (no marketplace; copy the skill into place)
git clone https://github.com/sdsrss/moa-skill.git
cp -r moa-skill/skills/moa ~/.claude/skills/moa      # Claude Code auto-discovers ~/.claude/skills/
```

```bash
# Runtime dependency either way (the HTTP layer is pure stdlib)
pip install pyyaml
# Optional CH2 (local CLI channel): install the codex CLI (0.144+) and log in
```

**Configure API keys via environment variables only** — never written to disk, logs, or reports:

```bash
export OPENROUTER_API_KEY=...      # recommended: one key reaches every provider
# or: export OPENAI_API_KEY=...    # any OpenAI-compatible endpoint

cp skills/moa/assets/config.example.yaml config.yaml   # then edit models/channels
```

`config.yaml` defines members (`name` / `seat` / `channel` / `model` / `fallback` / `timeout`) and `options` (`max_tokens_member` / `min_successful_members` / `grace_seconds`). The arbiter is your current agent — **it is not in the config** and makes no external calls. Model IDs churn fast; verify once with `dry-run` before a real run. The script auto-detects `http_proxy` / `https_proxy` / `no_proxy` and routes API calls through a proxy when present.

---

## Features

### Three dispatch channels (hybrid, cost-tiered)

| Channel | What | Billing | Dispatched by |
|---|---|---|---|
| **CH3 API** | OpenRouter (one key, all providers) / any OpenAI-compatible endpoint | per-token | `moa.py` |
| **CH2 CLI** | `codex exec` non-interactive (`-s read-only`, prompt via stdin) | codex subscription | `moa.py` |
| **CH1 Subagent** | Claude subagent (Task tool, can target a non-session model) | subscription | **arbiter (out-of-script)** |

`moa.py` runs CH2/CH3 seats; it skips `channel: subagent` (CH1) seats for the arbiter to dispatch in parallel via the Task tool, writing artifacts into the same `--collect-dir`.

### Three committee modes

- **`review`** — review / audit / second-opinion. Members take adversarial roles (feasibility / maintainability / security / user) and blind-review.
- **`decide`** — multi-option decisions. Members **claim an option** and argue it to the strongest while attacking rivals' fatal flaws (`references/roles-decide.md`).
- **`brainstorm`** — divergent personas generate ideas independently, **no refine round**, straight to curation.

### Composable interaction pipeline

**Generate** (independent answer / role-play) → **Refine** (anonymous peer-review · cross-examination · round-table discussion) → **Converge** (chair synthesis / arbitration / curation). Pick stages per scenario.

### Three convening scales

- **`full`** (manual `/moa <material>`) — fixed 4 top-tier seats + arbiter (3 families + a Self-MoA seat by default).
- **`auto`** (keyword / self-invoked) — orchestrator picks seat count, models, and pipeline by **scenario × difficulty × stage** (`references/routing.md`).
- **`custom`** (`--members N --models "id1,id2"`) — repeat one model = active **Self-MoA**.

### Reliability & safety, built in

Exponential backoff on 5xx/429 · one-shot JSON self-repair · dynamic quorum (`min(2, seats)`) with a 30 s grace window · degraded-roster flagging · secret-leak scanner over briefings and artifacts (`leak-check`) · sensitive-material egress warning before any external call.

---

## Highlights

- 🧩 **Truly independent second opinions** — up to four model families, blind generation, so blind spots are de-correlated instead of echoed (default ships three families + a Self-MoA seat; see the note above).
- ⚖️ **Arbiter with full context + anti-pollution clamps** — the aggregator is your main agent, kept honest by a statistics block, a falsification gate, and no-hedge / no-blocker-downgrade hard rules.
- 💸 **Cost-adaptive, not always-on** — L0 gate refuses trivial questions; `scenario × difficulty × stage` routing; `--dry-run` shows the roster, channels, proxy state, and cost estimate *before* you spend a token.
- 🛡️ **Anti-groupthink discipline stack** — blind isolation, anonymous cross-review, same-source consensus de-dup, sycophancy counter, three-state abstention, outlier protection, low-confidence hand-back.
- 🔁 **Self-MoA fallback** — no external channel? One strong model role-plays each seat across turns, with an explicit "role-diversity only, no cross-model de-correlation" disclosure.
- 🌐 **Bilingual, Chinese-first** — report prose follows the user's language; structured fields, config keys, and logs stay English.

---

## How It Compares

| Capability | Typical MoA / council tools | **MoA Skill** |
|---|---|---|
| Channel topology | single (all-API *or* all-CLI) | **hybrid** subagent + local CLI + API with fallback chain |
| Aggregator | re-call an API (no context) | **your agent with full context** + anti-pollution clamps |
| Cost control | one full run every time | **L0 gate + 3-D routing + dry-run** estimate |
| Interaction modes | fixed 1–3 flows | **7 composable stages**, pick per scenario |
| Triggers | manual only | manual + keyword + **agent self-invoke** |
| Anti-groupthink | 1–2 measures | **full discipline stack** (7+ measures) |
| Language | English only | **Chinese-first bilingual** |

---

## Usage

Four core subcommands: `dry-run` / `generate` / `refine` / `stats`. A typical review run:

```bash
R=moa-reports/run            # artifact dir (user-visible, committable)

# 0) Dry-run: roster, channels, proxy state, cost magnitude — show the user first
python skills/moa/scripts/moa.py dry-run --config config.yaml \
  --input $R/brief.md --mode review --refine-rounds 1

# 1) Generate: each member blind-reviews in parallel; lands once quorum is met
python skills/moa/scripts/moa.py generate --config config.yaml \
  --mode review --input $R/brief.md --collect-dir $R

# 2) Stats: mechanical tally (verdict votes, severity distribution, token usage, degraded flag)
python skills/moa/scripts/moa.py stats --config config.yaml \
  --mode review --collect-dir $R

# 3) Refine (optional, L2+; review/decide): see anonymized peers, three-state stance, revise
python skills/moa/scripts/moa.py refine --config config.yaml \
  --mode review --input $R/brief.md --collect-dir $R --round 1
python skills/moa/scripts/moa.py stats --config config.yaml \
  --mode review --collect-dir $R --round 1
```

Artifacts: `member_<name>.json` (per-member structured opinions), `stats.json` (mechanical stats), refine-round `member_<name>.r1.json` / `stats.r1.json`. **Convergence (chair synthesis / arbitration / curation) is done by the arbiter under the hard rules in `skills/moa/references/synthesis.md` — not inside the script.**

### Round-table discussion (optional; **L3 disputes + explicit user request only**)

Sequential speaking, later speakers see earlier turns (an explicit exception to blind review), multi-round + closing blind vote — highest cost, highest conformity risk. Use only for high-value irreversible decisions when the user asks to "really debate it" (lighter disagreements → use `refine`). The arbiter orchestrates round by round; `moa.py` provides `discuss-turn` / `discuss-prompt` / `discuss-blindvote` / `discuss-stats`. Three anti-conformity hedges: speaking-order rotation, per-turn `changed_by_new_argument` tagging (→ pseudo-discussion detection), and closing blind-vote drift detection. See [`references/discuss.md`](skills/moa/references/discuss.md).

> **Real end-to-end evidence** lives in [`moa-reports/`](moa-reports/): document review, multi-option decision, brainstorm, round-table discussion (2 rounds + blind vote), Self-MoA, fault injection, and an `auto` top-tier run (4 seats across all three channels; the 4th seat is a second OpenAI model as xAI was unavailable on the test key — not fully heterogeneous). Top-tier model slugs have gotchas — see the caveats in [`config.example.yaml`](skills/moa/assets/config.example.yaml).

---

## Cost

Tokens run a few × a single model. Measured (2 billed seats + 1 refine round, on cheap non-reasoning models — see [`COST-NOTE.md`](moa-reports/cost-m4/COST-NOTE.md)) = **4.79× baseline**. The **shipped default puts only seat C on a billed CH3 channel** (A=codex, B & D=subscription subagents), so the user-paid token multiple is lower still. **All-CH3 four seats + refine ≈ 9.6×** (over the 7× target). Always `dry-run` and show the estimate before a real run.

---

## FAQ

**What problem does MoA Skill solve?**
It gives a Claude Code agent an on-demand panel of independent, heterogeneous models for judgment-heavy tasks (review, decision, arbitration, brainstorming), so conclusions don't rest on one model's blind spots.

**When should I *not* use it?**
Simple Q&A and mechanically-verifiable objective problems (arithmetic, fact lookup) — MoA's measured gain there is negative. The L0 gate refuses to convene and hands back to the main model.

**Do I need multiple API keys?**
No. One `OPENROUTER_API_KEY` reaches every provider. Any OpenAI-compatible endpoint works via `OPENAI_API_KEY`. Claude seats can run on your subscription through the subagent channel (no extra key).

**Is my key or my material safe?**
Keys are read from environment variables only — never written to disk, logs, or reports; `leak-check` statically scans artifacts for accidental leaks. Review material is sent to every third-party provider in your config, so `dry-run`/`generate` auto-scan the briefing for suspected secrets and warn before egress. For fully local runs, use `custom` with all CH1/CH2 seats.

**What if a model call fails?**
Transient errors (5xx/429) get exponential-backoff retries; malformed JSON gets one self-repair; a seat with a fallback drops down its chain. If successful members fall below `min(2, seats)`, the run aborts — "a conclusion from too few advisors doesn't deserve to be called a committee review."

**What if all external channels are unavailable?**
It degrades to **Self-MoA**: one strong model role-plays each seat across turns, with a mandatory disclosure that you get role-diversity gains only, not cross-model de-correlation.

**Why is the arbiter the current agent and not another API call?**
Per the MoA paper, aggregators benefit from full context while proposers don't — a weak, context-free aggregator is catastrophic. The arbiter never downgrades; the fallback chain only reduces member seats.

**Common errors** — empty-response shells (raise `max_tokens_member` ≥ 8000 for reasoning models), `404 No allowed providers` (verify the model ID / provider with `dry-run`), and more, are covered with fixes in the [troubleshooting notes](skills/moa/references/) and `config.example.yaml`.

---

## Development

```bash
python -m pytest skills/moa/tests/ -q      # 125 cases (behavior + doc-consistency fixtures), no network
python skills/moa/scripts/moa.py leak-check # static secret-leak self-check: non-zero exit on hit (preview redacted)
```

Layout: `skills/moa/` — `SKILL.md` · `references/` ×7 (incl. `discuss.md`) · `scripts/moa.py` · `assets/config.example.yaml` · `tests/`. Only the manifest and `skills/` are loaded at runtime; `docs/`, `moa-reports/`, and `README*` are dev/record material.

---

## License

[MIT](LICENSE) © sds.rs
