# MoA Skill — 多模型委员会（Claude Code 技能）

[English](README.md) · **中文**

> 一个 [Claude Code](https://docs.claude.com/en/docs/claude-code) 技能：把你的主 agent 变成**五模型委员会的主席**。最多 4 个异构大模型委员并行评审 / 决策 / 头脑风暴——**并行独立盲审 → 结构化互动 → 证据驱动收敛**——产出比任何单一模型更可靠的结论。

原理基于 **Mixture-of-Agents（MoA）**：不同模型盲点不同，独立盲审 + 结构化聚合能突破单模型上限。**仅对 LLM-judge 型主观任务有正收益**——简单问答与可机械验证的客观问题（算术 / 事实检索）不要用。

<p>
<img alt="status" src="https://img.shields.io/badge/status-v1.3.2-brightgreen"> <img alt="tests" src="https://img.shields.io/badge/tests-126%20passing-brightgreen"> <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-blue"> <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

## 目录

- [为什么用它](#为什么用它)
- [安装](#安装)
- [功能说明](#功能说明)
- [优秀亮点](#优秀亮点)
- [差异对比](#差异对比)
- [使用说明](#使用说明)
- [成本](#成本)
- [常见问题](#常见问题)
- [开发](#开发)
- [License](#license)

---

## 为什么用它

一个模型评审自己的产出，只有一套盲点。MoA Skill 召集 **至多 4 个独立异构模型**（横跨 OpenAI / Anthropic / Google / xAI 家族），生成阶段互不可见对方原始产出，再由**持完整上下文的当前 agent 仲裁**，受一套反群体思维硬规则约束。你得到的是**真正独立**的第二、第三、第四意见，分歧被保留而非被磨平。

> **关于默认阵容：** 发货的 `config.example.yaml` 实际是三家族——OpenAI(codex) + Anthropic(Opus) + Google(Gemini)——外加第四席**角色分化的 Self-MoA**(第二个 Anthropic 模型扮反方)。第四个*家族* xAI/Grok 是可选项：需要有 x-ai 供给的 OpenRouter key(很多 key 对 Grok 返回 list-only 404)，按 [`config.example.yaml`](skills/moa/assets/config.example.yaml) 的备忘替换即可。家族数是配置选择，并非硬编码为四。

它是**随叫随到的委员会，不是自动开发流水线**。编码与修复仍归主 agent；MoA 只做判断力密集节点：评审、决策、仲裁、头脑风暴。

---

## 安装

两种安装方式。**Marketplace**（v1.3.0 起支持）是一行命令；**直接拷贝**是零依赖兜底。

```bash
# 方式 A —— Claude Code marketplace（推荐）
/plugin marketplace add sdsrss/moa-skill
/plugin install moa@moa-skill

# 方式 B —— 直接拷贝（不走 marketplace，把 skill 拷到位）
git clone https://github.com/sdsrss/moa-skill.git
cp -r moa-skill/skills/moa ~/.claude/skills/moa      # Claude Code 自动发现 ~/.claude/skills/
```

```bash
# 两种方式都需要的运行依赖（HTTP 层为纯标准库）
pip install pyyaml
# 可选 CH2（本地 CLI 通道）：装 codex CLI（0.144+）并完成 codex 侧登录
```

**API key 一律走环境变量**——不落盘、不进日志 / 报告：

```bash
export OPENROUTER_API_KEY=...      # 推荐：一个 key 覆盖所有厂商
# 或 export OPENAI_API_KEY=...     # 任意 OpenAI 兼容端点

cp skills/moa/assets/config.example.yaml config.yaml   # 首次；按需改模型 / 通道
```

`config.yaml` 定义委员（`name` / `seat` / `channel` / `model` / `fallback` / `timeout`）与 `options`（`max_tokens_member` / `min_successful_members` / `grace_seconds`）。仲裁人 = 当前 agent，**不在配置里**，不走外部调用。模型 ID 迭代快，正式跑前用 `dry-run` 核对一次。脚本自动读取 `http_proxy` / `https_proxy` / `no_proxy`，检测到代理时 API 调用优先走代理。

---

## 功能说明

### 三个派发通道（混合、成本分层）

| 通道 | 说明 | 计费 | 由谁派发 |
|---|---|---|---|
| **CH3 API** | OpenRouter（一个 key 调所有厂商）/ 任意 OpenAI 兼容端点 | 按 token 计费 | `moa.py` |
| **CH2 CLI** | `codex exec` 非交互（`-s read-only`、prompt 走 stdin） | 走 codex 订阅 | `moa.py` |
| **CH1 子代理** | Claude 子代理（Task 工具，可指定非会话默认模型） | 走订阅 | **仲裁人脚本外派发** |

`moa.py` 只跑 CH2/CH3 席位；`channel: subagent`（CH1）席位它会跳过，留给仲裁人用 Task 工具并行派发、产物写入同一 `--collect-dir`。

### 三种委员会模式

- **`review`**——评审 / 审查 / 二次确认。委员各领对抗角色（feasibility / maintainability / security / user）盲审。
- **`decide`**——多选项决策。委员**认领选项**并论证到最强，同时攻击对手致命弱点（`references/roles-decide.md`）。
- **`brainstorm`**——发散人格独立产点子，**无精炼轮**，直接进策展收敛。

### 可组合互动流水线

**生成**（独立回答 / 角色扮演）→ **精炼**（匿名互评 · 交叉审查 · 开会讨论）→ **收敛**（主席综合 / 仲裁 / 策展）。按场景选装。

### 三种召集规模

- **`full`**（手动 `/moa <材料>`）——固定 4 席顶配 + 仲裁（默认 3 家族 + 1 席 Self-MoA）。
- **`auto`**（关键词 / 自调触发）——编排器按 **场景 × 难度 × 阶段** 选人数 / 模型 / 流水线（`references/routing.md`）。
- **`custom`**（`--members N --models "id1,id2"`）——重复同一模型 = 主动 **Self-MoA**。

### 内建可靠性与安全

5xx/429 指数退避重试 · JSON 一次性自修复 · 动态法定人数（`min(2, 席位数)`）+ 30s 宽限窗 · degraded 阵容标记 · 简报与产物密钥泄漏静态扫描（`leak-check`）· 外发前敏感材料告警。

---

## 优秀亮点

- 🧩 **真正独立的第二意见**——四个模型家族、盲审生成，盲点去相关而非互相回声。
- ⚖️ **持完整上下文的仲裁人 + 反污染钳制**——聚合者是你的主 agent，由统计块、证伪门、禁折中 / 禁降级 blocker 硬规则约束。
- 💸 **成本自适应，不是常开**——L0 闸门拒绝琐碎问题；`场景 × 难度 × 阶段` 路由；`--dry-run` 在花第一个 token 前展示阵容、通道、代理状态与成本估算。
- 🛡️ **反群体思维纪律栈**——盲审隔离、匿名互评、同源共识去重、谄媚计数器、三态弃权、孤例保护、低置信退还决定权。
- 🔁 **Self-MoA 兜底**——无外部通道时，单个强模型分回合扮演各席，并显式声明"只有角色分化收益，无跨模型去相关收益"。
- 🌐 **中文优先双语**——报告正文跟随用户语言；结构化字段、配置键、日志保持英文。

---

## 差异对比

| 能力 | 典型 MoA / council 工具 | **MoA Skill** |
|---|---|---|
| 通道拓扑 | 单通道（纯 API *或* 纯 CLI） | **混合**：子代理 + 本地 CLI + API + fallback 链 |
| 聚合者 | 再调一次 API（无上下文） | **持完整上下文的你的 agent** + 反污染钳制 |
| 成本控制 | 每次全量跑 | **L0 闸门 + 三维路由 + dry-run** 估算 |
| 互动模式 | 固定 1–3 条流程 | **7 种可组合阶段**，按场景选 |
| 触发方式 | 仅手动 | 手动 + 关键词 + **主 agent 自调** |
| 反群体思维 | 1–2 条 | **完整纪律栈**（7+ 条） |
| 语言 | 仅英文 | **中文优先双语** |

---

## 使用说明

四个核心子命令：`dry-run` / `generate` / `refine` / `stats`。典型评审流程：

```bash
R=moa-reports/run            # 产物目录（用户可见、可提交）

# 0) 预演：看委员构成、通道、代理状态、成本量级，先给用户过目
python skills/moa/scripts/moa.py dry-run --config config.yaml \
  --input $R/brief.md --mode review --refine-rounds 1

# 1) 生成：各委员独立盲审（并行），达法定人数即落盘
python skills/moa/scripts/moa.py generate --config config.yaml \
  --mode review --input $R/brief.md --collect-dir $R

# 2) 统计：机械汇总（裁决计票、严重度分布、token 用量、degraded 标记）
python skills/moa/scripts/moa.py stats --config config.yaml \
  --mode review --collect-dir $R

# 3) 精炼轮（可选，L2+；review/decide）：看匿名化他人意见，三态表态并修订
python skills/moa/scripts/moa.py refine --config config.yaml \
  --mode review --input $R/brief.md --collect-dir $R --round 1
python skills/moa/scripts/moa.py stats --config config.yaml \
  --mode review --collect-dir $R --round 1
```

产物：`member_<name>.json`（逐委员结构化意见）、`stats.json`（机械统计）、精炼轮 `member_<name>.r1.json` / `stats.r1.json`。**收敛（主席综合 / 仲裁 / 策展）由仲裁人按 `skills/moa/references/synthesis.md` 硬规则完成，不在脚本内。**

### 开会讨论（可选精炼阶段；**仅 L3 复杂争议 + 用户显式要求**）

顺序发言、后发者可见前发言（盲审的显式例外）、多轮 + 收尾盲投——成本最高、从众风险最高，只在高价值不可逆决策且用户明确要"真辩一轮"时用（轻量分歧用 `refine` 即可）。由仲裁人**逐回合编排**，`moa.py` 提供 `discuss-turn` / `discuss-prompt` / `discuss-blindvote` / `discuss-stats` 四个助手。三重反从众对冲：发言序每轮轮转、每回合 `changed_by_new_argument` 标注（→ 假讨论检测）、收尾盲投漂移检测。详见 [`references/discuss.md`](skills/moa/references/discuss.md)。

> **真实端到端证据**见 [`moa-reports/`](moa-reports/)：文档评审、多选项决策、头脑风暴、开会讨论（2 轮 + 盲投）、Self-MoA、故障注入，以及一次 `auto` 顶配实跑（4 席 · 三通道；第 4 席因测试 key 无 xAI 供给而用了第二个 OpenAI 模型，非完全异构）。顶配模型 slug 有坑，正式跑前对照 [`config.example.yaml`](skills/moa/assets/config.example.yaml) 的备忘核对。

---

## 成本

Token 约为单模型的数倍。实测（2 个计费席 + 1 精炼轮，用的是便宜的非推理模型，见 [`COST-NOTE.md`](moa-reports/cost-m4/COST-NOTE.md)）= **4.79× 基线**。**发货默认仅 C 席走计费 CH3**（A=codex、B 与 D=订阅子代理），用户实付倍数比这更低。**全 CH3 四席 + 精炼轮 ≈ 9.6×**（超 7× 目标）。永远先 `dry-run` 给用户看成本估算再正式跑。

---

## 常见问题

**它解决什么问题？**
给 Claude Code agent 一个随叫随到的独立异构模型委员会，用于判断力密集任务（评审 / 决策 / 仲裁 / 头脑风暴），让结论不押在单一模型的盲点上。

**什么时候*不*该用？**
简单问答与可机械验证的客观问题（算术 / 事实检索）——MoA 对此类的实测收益为负。L0 闸门会拒绝召集并退回主模型。

**需要多个 API key 吗？**
不需要。一个 `OPENROUTER_API_KEY` 覆盖所有厂商；任意 OpenAI 兼容端点走 `OPENAI_API_KEY`。Claude 席位可经子代理通道走订阅（无需额外 key）。

**我的 key 与材料安全吗？**
key 只从环境变量读取——不落盘、不进日志 / 报告；`leak-check` 静态扫描产物防误落盘。评审材料会发送至配置里所有第三方提供商，故 `dry-run`/`generate` 会自动扫描简报中的疑似密钥并在外发前告警。纯本地场景用 `custom` 全 CH1/CH2 席。

**某个模型调用失败怎么办？**
瞬态错误（5xx/429）指数退避重试；非法 JSON 一次自修复；配了 fallback 的席位沿链降级。成功委员数 < `min(2, 席位数)` 时中止——"顾问不足的结论不配称为委员会评审"。

**所有外部通道都不可用？**
降级为 **Self-MoA**：单个强模型分回合扮演各席，并强制声明只有角色分化收益、无跨模型去相关收益。

**为什么仲裁人是当前 agent 而不是再调一次 API？**
按 MoA 论文，聚合者受益于完整上下文而提案者不然——弱的、无上下文的聚合者会灾难性拉胯。仲裁人永不降级；fallback 链只降委员席位。

**常见报错**——空壳返回（推理模型把 `max_tokens_member` 提到 ≥ 8000）、`404 No allowed providers`（用 `dry-run` 核对模型 ID / provider）等，含修复见下表与 `config.example.yaml`。

### Troubleshooting

| 现象 | 原因 | 处理 |
|---|---|---|
| `FAIL[empty]: empty response shell` | 推理型模型把 `max_tokens` 预算耗在推理上，正文为空 | 调大 `max_tokens_member`，或该席改用非推理模型 |
| `FAIL[client]: HTTP 404 No allowed providers` | 该模型在你的 OpenRouter 账号无可用 provider，或模型 ID 过期 | `dry-run` 核对 ID；换 provider 可用的模型；配 fallback 席 |
| 顶配推理模型（gpt-5 / gemini-pro）跑很久后 `FAIL[empty]` | 推理吃光 `max_tokens`，正文空壳（**假阴性**，模型其实可用） | 把 `max_tokens_member` 提到 ≥ 8000（decide 长输出更需要）|
| 模型在 OpenRouter 清单里但调用 404 | list-only：该 key 无供给 | 换 key 上真服务的模型；过代理后仍 404 = 真不服务，非代理问题 |
| `codex ... gpt-5-codex not supported ... ChatGPT account` | codex（ChatGPT 账号）不接受显式 `-m gpt-5-codex` | CH2 席**省略 `model`**，用 codex 默认（GPT-5 级）|
| `FAIL: output not parseable` | 模型返回非 JSON（部分便宜模型 JSON 遵从差） | 走 fallback 链或自修复；换 JSON 遵从更好的模型 |
| `[abort] successful members < required` | 成功委员数 < `min(min_successful_members, 席位数)` | 检查 key/额度/模型可用性；阈值运行时对席位数取 min，不误杀 L1 单委员 |
| `codex not found on PATH` | CH2 席位但未装 codex | 装 codex 或把该席 `channel` 改成 `api`，或配 fallback |
| `channel=subagent must be dispatched by arbiter` | CH1 席位无 api/cli fallback，`moa.py` 不派发 | 由仲裁人用 Task 工具派发，或给该席配 api fallback |
| 报告数字与 `stats` 不一致 | 仲裁人凭印象改写了共识度 / 数量 | 硬规则要求报告数字与 `stats` 一致；`sycophancy_alert` 为真须声明并下调置信度 |

---

## 开发

```bash
python -m pytest skills/moa/tests/ -q      # 126 项(行为测试 + 文档一致性校验)，无网络
python skills/moa/scripts/moa.py leak-check # 密钥泄漏静态自查：命中即非零退出（预览脱敏）
```

目录：`skills/moa/` — `SKILL.md` · `references/` ×7（含 `discuss.md`）· `scripts/moa.py` · `assets/config.example.yaml` · `tests/`。运行时只加载清单与 `skills/`；`docs/`、`moa-reports/`、`README*` 为开发 / 记录材料。

---

## License

[MIT](LICENSE) © sds.rs
