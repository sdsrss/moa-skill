# MoA Skill — 多模型委员会

把当前 agent（仲裁人，持完整上下文）与最多 4 个异构大模型委员组成"五模型委员会"，对**判断力密集型任务**（评审 / 决策 / 推荐 / 头脑风暴，及审核/审查/分析/测试问题的二次确认）做 **并行独立生成 → 结构化互动 → 证据驱动收敛**，产出比单一模型更可靠的结论。

原理基于 Mixture-of-Agents：不同模型盲点不同，独立盲审 + 结构化聚合能突破单模型上限。**仅对 LLM-judge 型主观任务有正收益**——简单问答与可机械验证的客观问题（算术/事实检索）不要用。

> 状态：**v1.0（M1–M4 已实现并经真实 API 验证）**。需求/设计/计划等开发文档为仓库内部材料，未随发布；角色契约与硬规则见 [`skills/moa/references/`](skills/moa/references/)。

## 三个通道

| 通道 | 说明 | 计费 | 由谁派发 |
|---|---|---|---|
| **CH3 API** | OpenRouter（一个 key 调所有厂商）/ OpenAI 兼容端点 | 按 token 计费 | `moa.py` |
| **CH2 CLI** | codex exec 非交互（`-s read-only`、prompt 走 stdin） | 走 codex 订阅 | `moa.py` |
| **CH1 子代理** | Claude 子代理（Task 工具，可指定非会话默认模型） | 走订阅 | **仲裁人脚本外派发** |

`moa.py` 只跑 CH2/CH3 席位；`channel: subagent`（CH1）席位它会跳过，留给仲裁人用 Task 工具并行派发、产物写入同一 `--collect-dir`。

## 安装

```bash
pip install pyyaml            # 唯一依赖；HTTP 层为纯标准库
# CH2 可选: 本机装 codex CLI（codex-cli 0.144+）并完成 codex 侧登录
```

## 配置

key **一律走环境变量，不落盘、不进日志/报告**：

```bash
export OPENROUTER_API_KEY=...      # 推荐:一个 key 覆盖所有厂商
# 或 export OPENAI_API_KEY=...     # OpenAI 兼容端点

cp skills/moa/assets/config.example.yaml config.yaml   # 首次;按需改模型/通道
```

`config.yaml` 定义委员（name / seat / channel / model / fallback / timeout）与 `options`
（`max_tokens_member` / `min_successful_members` / `grace_seconds` / `max_refine_rounds`）。
仲裁人 = 当前 agent，**不在配置里**，不走外部调用。模型 ID 迭代快，正式跑前用 `dry-run` 核对一次。

代理：脚本自动读取 `http_proxy` / `https_proxy` / `no_proxy`，检测到代理时 API 调用优先走代理。

## 用法

命令行四个子命令：`dry-run` / `generate` / `refine` / `stats`。典型评审流程：

```bash
R=moa-reports/run           # 产物目录(用户可见、可提交)

# 0) 预演:看委员构成、通道、代理状态、成本量级,先给用户过目
python skills/moa/scripts/moa.py dry-run --config config.yaml \
  --input $R/brief.md --mode review --refine-rounds 1

# 1) 生成:各委员独立盲审(并行),达法定人数即落盘
python skills/moa/scripts/moa.py generate --config config.yaml \
  --mode review --input $R/brief.md --collect-dir $R

# 2) 统计:机械汇总(裁决计票、严重度分布、token 用量、degraded 标记)
python skills/moa/scripts/moa.py stats --config config.yaml \
  --mode review --collect-dir $R

# 3) 精炼轮(可选,L2+;review/decide):看匿名化他人意见,三态表态并修订
python skills/moa/scripts/moa.py refine --config config.yaml \
  --mode review --input $R/brief.md --collect-dir $R --round 1
python skills/moa/scripts/moa.py stats --config config.yaml \
  --mode review --collect-dir $R --round 1
```

产物：`member_<name>.json`（逐委员结构化意见）、`stats.json`（机械统计）、精炼轮 `member_<name>.r1.json` / `stats.r1.json`。**收敛（主席综合 / 仲裁 / 策展）由仲裁人按 `skills/moa/references/synthesis.md` 硬规则完成，不在脚本内。**

### 三种模式

- **评审 `--mode review`**：评审 / 审查 / 二次确认。委员各领对抗角色盲审（feasibility / maintainability / security / user）。
- **决策 `--mode decide`**：多选项决策。委员按 `references/roles-decide.md` **认领选项对抗论证**；认领角色由仲裁人在 config `custom_roles` 里按选项注入。
- **头脑风暴 `--mode brainstorm`**：发散人格独立产点子，**无精炼轮**，直接进策展收敛。

### 三种召集规模（由 SKILL.md 流程决定）

- **顶配 full**（手动 `/moa <材料>`）：固定 4 席顶配异构 + 仲裁。
- **智能 auto**（关键词/自调触发）：按 `references/routing.md` 三步（场景×难度×阶段）选人数/模型/流水线。
- **自定义 custom**（`--members N --models "id1,id2"`）：重复同一模型 = 主动 Self-MoA。

## 成本

Token 约为单模型的数倍。M4 实测（2 席 + 1 精炼轮）= **4.79× 基线**；默认 config 因 A/B 走订阅通道，用户实付倍数 ≈ 4.79× ≤ 7× 目标。**全 CH3 四席 + 精炼轮 ≈ 9.6×**（超标，见 [`moa-reports/cost-m4/COST-NOTE.md`](moa-reports/cost-m4/COST-NOTE.md)）。永远先 `dry-run` 给用户看成本估算再正式跑。

## Troubleshooting

| 现象 | 原因 | 处理 |
|---|---|---|
| `FAIL[empty]: empty response shell` | 推理型模型（如 `gpt-5-nano`）把 `max_tokens` 预算耗在推理上，正文为空 | 调大 `max_tokens_member`，或该席改用非推理模型 |
| `FAIL[client]: HTTP 404 No allowed providers` | 该模型在你的 OpenRouter 账号无可用 provider，或模型 ID 过期 | `dry-run` 核对 ID；换 provider 可用的模型；配 fallback 席 |
| `FAIL: output not parseable` | 模型返回非 JSON（部分便宜模型 JSON 遵从差） | 走 fallback 链或自修复；换 JSON 遵从更好的模型 |
| `[abort] successful members < required` | 成功委员数 < `min(min_successful_members, 席位数)` | 检查 key/额度/模型可用性；`min_successful_members` 运行时会对席位数取 min，不会误杀 L1 单委员 |
| `codex not found on PATH` | CH2 席位但未装 codex | 装 codex 或把该席 `channel` 改成 `api`，或配 fallback |
| `channel=subagent must be dispatched by arbiter` | CH1 席位无 api/cli fallback，`moa.py` 不派发 | 由仲裁人用 Task 工具派发，或给该席配 api fallback |
| 报告数字与 `stats` 不一致 | 仲裁人凭印象改写了共识度/数量 | 硬规则要求报告数字与 `stats` 一致；`sycophancy_alert` 为真须声明并下调置信度 |

## 使用纪律

- 报告的分歧点需**人工裁决**；全员一致 ≠ 零风险（各家训练数据重叠，存在共同盲区，免责声明勿删）。
- 材料含敏感信息时提醒用户：**将发送至配置中所有第三方模型提供商**。纯本地场景可 custom 全 CH1/CH2。`dry-run` 与 `generate` 会**自动扫描简报中的疑似密钥/凭据**并打脱敏告警（不阻断），检出请复核后再外发。
- 无任何外部通道可用时降级为 **Self-MoA**（同一强模型多角色分回合扮演），必须声明"只有角色分化收益，无跨模型去相关收益"。

## 开发

```bash
python -m pytest skills/moa/tests/ -q      # 81 项单元/集成/触发/路由/故障注入/安全用例,无网络
python skills/moa/scripts/moa.py leak-check # 密钥泄漏静态自查:命中即非零退出(预览脱敏)
```

目录：`skills/moa/`（`SKILL.md` · `references/`×6 · `scripts/moa.py` · `assets/config.example.yaml` · `tests/`）。

## License

MIT，见 [LICENSE](LICENSE)。

## 分发形态（v1.0 插件）

本仓采用**插件仓布局**：`.claude-plugin/plugin.json`（清单）+ `skills/moa/`（skill 本体，`skills/` 由 Claude Code 自动发现）。

**打包边界——Claude Code 只加载"清单 + `skills/`"，其余仓根内容对运行时惰性：**

| 随插件加载（运行时表面） | 仅开发/记录，不被加载 |
|---|---|
| `.claude-plugin/plugin.json` | `docs/`、`CLAUDE.md`（**已 gitignore：仅本地开发材料，不入库、不发布**） |
| `skills/moa/`（SKILL.md / references / scripts / assets / tests） | `moa-reports/`（**运行输出目录**，相对用户 cwd 生成，非仓内容） |
| | `README.md`（在仓，不被加载）· `.git/` · `.code-graph/` · `.claude/`（本地状态） |

- skill 运行时对 `docs/` 与 `moa-reports/` **零加载依赖**，且 `docs/`、`CLAUDE.md` 已由 `.gitignore` 排除出仓库——设计/需求/计划文档留在维护者本地。
- `.gitignore` 另排除可再生产物与本地配置（`__pycache__` / `.pytest_cache` / 各 lint 缓存 / `.venv/` / `.env*` / 根 `config.yaml` / `moa-reports/**/member_*.json` / `stats*.json`），保留 `moa-reports/cost-m4/` 的 `COST-NOTE.md`/`brief.md`/`config.yaml` 作可复现证据样例。

安装：作为插件被识别后按插件装；或不装插件、直接把 `skills/moa/` 拷到 `~/.claude/skills/moa/`。
