# MoA Skill 架构设计文档

> 版本 v1.0 · 2026-07-11 · 状态：**M1–M3 已实现并经真实 API 验证 · M4 收尾中**（两阶段协议、三通道、fallback、Quorum 宽限窗、按模式统计、错误分类均已落地于 `skills/moa/scripts/moa.py`）
> 上游：[requirements.md](requirements.md) · 下游：[development-plan.md](development-plan.md)
> 主要借鉴：参考插件 1（model-council，盲审/聚合硬规则/双协议/代理基线）、方案建议 1（证据仲裁/停止条件）、方案建议 2（MoA 岛屿/黑板/角色透镜）、Hermes MoA 2.0（剥离上下文的委员 + 带上下文的聚合者）；7 个 GitHub 参考项目的实仓采纳明细见 [reference-adoption.md](reference-adoption.md)

---

## 1. 总体架构

```mermaid
flowchart TD
    U["用户 / 关键词 / 主agent自调"] --> E["入口: /moa full|auto|custom"]
    E --> O["编排器(SKILL.md 指导下的当前agent)\n场景×难度×阶段 → 人数/模型/流水线"]
    O -->|L0| X["拒绝启动, 主模型直答"]
    O --> B["简报构建: 自包含 brief.md"]
    B --> C1["CH1 子代理(Claude系)\n仲裁人经 Task 机制直接派发"]
    B --> D["scripts/moa.py --phase generate\nCH2 本地CLI + CH3 HTTP API"]
    C1 --> M["屏障: collect-dir 收齐\n全部 member_*.json"]
    D --> M
    M -->|"精炼 0-2 轮(双路径同上,\n输入=全部委员匿名意见)"| M
    M --> ST["moa.py --phase stats\n(对合流后全部产物机械统计)"]
    ST --> S["收敛(当前agent):\n主席综合 / 仲裁"]
    S --> P["报告 report_*.md + council_*.json"]
```

职责划分的核心原则（来自 Hermes MoA 成本结构 + 方案建议 1 权限表）：

| 角色 | 上下文 | 权限 | 职责 |
|---|---|---|---|
| 仲裁人（当前主 agent） | 完整对话 + 工具 | 读写报告 | 路由、简报、召集、综合/仲裁、呈现 |
| 委员（≤4） | 仅自包含简报 | 只读、无工具 | 独立生成、精炼修订，输出结构化 JSON |
| 分发脚本 | 无模型智能 | 网络调用 | 通道探测、并行调度、重试、JSON 修复、统计 |

**当前 agent 兼任仲裁人**而非再调一次 API 聚合，理由：(a) 它拥有完整上下文与工具，能校验委员的事实断言；(b) 省一次顶级模型调用；(c) 与触发方式 T3（主 agent 自调）天然衔接。代价是"自己综合自己召集的评审"存在立场污染风险——用 §8 聚合硬规则（机械可核查）对冲。

## 2. 技能目录结构

```text
moa-skill/
├── .claude-plugin/
│   └── plugin.json               # 后续封装为 plugin 时启用
└── skills/
    └── moa/
        ├── SKILL.md              # 触发条件 + 模式路由 + 编排流程（保持精简，细节渐进加载）
        ├── references/
        │   ├── routing.md        # 场景×难度×阶段 → 流水线/人数/模型 决策表
        │   ├── roles-review.md   # 评审对抗角色（安全审计/可行性质疑/用户代言/成本魔鬼）
        │   ├── roles-decide.md   # 决策认领角色 + 交叉审查指令
        │   ├── roles-brainstorm.md # 发散人格
        │   ├── synthesis.md      # 主席综合/仲裁硬规则 + 证据等级
        │   └── briefing.md       # 简报模板与写法（自包含性检查清单）
        ├── scripts/
        │   └── moa.py            # 分发器：通道探测/并行调度/重试/JSON修复/统计/dry-run
        └── assets/
            └── config.example.yaml
```

渐进加载纪律：SKILL.md 只含路由逻辑与调用方法（目标 ≤ 500 行）；角色契约、硬规则、模板放 `references/` 按需读取；确定性逻辑全部进 `scripts/`。

## 3. 编排决策（auto 模式）

编排器 = 当前 agent 按 SKILL.md + `references/routing.md` 做的三步判断，全部在召集前完成并（非 dry-run 时也）向用户一句话公示："场景=决策，难度=L2，阶段=架构 → 3 委员（A/C/D），流水线=认领→交叉审查→仲裁"。

**Step 1 场景识别**：评审 / 决策 / 头脑风暴 / 二次确认 / 总结评审（关键词 + 材料形态）。多场景复合时拆为两次调用（先 review 后 brainstorm）。

**Step 2 难度评估** → 委员数与轮数（requirements §7 表）。判据：错误代价（可逆性）、分歧空间（合理答案是否多个）、材料复杂度。低于 L1 拒绝启动。

**Step 3 阶段识别** → 委员侧重（requirements §8 表），决定 A/B/C/D 中激活哪几席。

模型分配在席位确定后查通道可用性表（§4）落到具体模型；`full` 模式跳过 Step 2/3 的裁员逻辑，固定 4 席顶配；`custom` 模式完全按用户参数。

## 4. 三通道分发器

### 4.1 通道抽象

```yaml
# config.yaml 中每个委员的通道配置
members:
  - name: reasoning-a
    seat: A                        # A|B|C|D，决定默认角色映射(routing.md)
    role: feasibility_skeptic      # 可省略,默认按 seat×场景 查 roles-*.md
    channel: cli                   # subagent | cli | api
    cli_cmd: "codex exec --model gpt-5.6-sol"   # channel=cli 时
    model: gpt-5.6-sol
    fallback:                      # 通道降级链，按序尝试
      - {channel: api, protocol: openrouter, model: openai/gpt-5.6-sol}
  - name: coding-b
    seat: B
    channel: subagent              # 经 Claude Code Task 机制派发
    model: claude-opus-4-8
    fallback:
      - {channel: api, protocol: openrouter, model: anthropic/claude-opus-4.8}
  - name: fact-c
    seat: C
    channel: api
    protocol: openrouter           # openrouter | openai(兼容端点，可自带 base_url/api_key_env)
    model: google/gemini-3.1-pro-preview
  - name: adversarial-d
    seat: D
    channel: api
    protocol: openrouter
    model: x-ai/grok-4.5
aggregation:
  by: current-agent                # 仲裁人=当前agent，不走API
options:
  max_tokens_member: 3000
  timeout_seconds: 180
  min_successful_members: 2        # 运行时取 min(此值, 实际席位数)
  max_refine_rounds: 2
custom_roles: {}                   # 内联自定义角色。member.role 解析顺序:
                                   # custom_roles > 场景 roles-*.md > seat 默认映射
```

### 4.2 各通道实现要点

| 通道 | 实现 | 探测方法 | 备注 |
|---|---|---|---|
| CH1 subagent | 仲裁人用 Task/Agent 工具派发子代理，提示词=角色契约+简报，要求返回纯 JSON。**工具约束**：派发时选用最小工具集，角色契约明令"不得调用工具、不得读写文件，仅基于简报作答"，落实 requirements §11"委员只读"要求 | 恒可探测（Claude Code 环境内）；Agent 工具支持指定非会话默认模型（development-plan Q1 已拍板），调用失败仍走 fallback | 唯一不经 `moa.py` 的通道；结果由仲裁人写入 `collect-dir` 与其他委员结果合流 |
| CH2 cli | `moa.py` 以子进程调用 codex 非交互模式，**prompt 经 stdin/临时文件传递（禁走 argv，防 ARG_MAX 与注入）**，并加 `--sandbox read-only` 级权限约束；stdout 截取 JSON | `shutil.which("codex")` | 走 CLI 自身鉴权；per-member 独立超时；"成功外壳但空响应"按瞬态失败重试一次（工程红线见 reference-adoption.md §4） |
| CH3 api | 标准库 `urllib` POST `chat/completions`；openrouter 协议自动补 base_url/归因头/`OPENROUTER_API_KEY`，openai 协议支持自定义 `base_url`/`api_key_env`（兼容 DeepSeek/Moonshot/vLLM 等） | 对应 key 环境变量存在 | 依赖仅 `pyyaml`，HTTP 纯标准库（沿用参考插件 1 方案） |

**代理策略**（继承参考插件 1 实现）：启动时读 `http_proxy/https_proxy/no_proxy`；有代理则 LLM 调用优先走代理并打印一次提示；`localhost/127.0.0.1/::1` 与 no_proxy 域名强制直连（保护内网 base_url）。

**降级链**：委员按 `fallback` 序尝试通道 → 全失败则该席位缺席 → 存活席位 < `min_successful_members` 时：有任何外部通道可用则中止报错；**一个都没有**则提议 Self-MoA 模式（需用户确认或 auto 模式下直接执行+报告声明）。Self-MoA 由仲裁人分回合独立扮演各角色，每回合只依据简报+角色契约，产出写独立文件后再统一收敛。

### 4.3 混合并行时序（两阶段协议）

`moa.py` 按阶段调用：`--phase generate|refine|stats`，全部以 `--collect-dir <dir>` 为共享产物目录——每委员一个 `member_<name>.json`，精炼轮产物 `member_<name>.r<N>.json`；另支持 `--member <name>` 子集运行。时序：

1. **生成**：仲裁人后台启动 `moa.py --phase generate`（CH2/CH3 委员），同时并行派发 CH1 子代理；
2. **屏障 1**：收齐全部生成产物（或超时按缺席处理）；
3. **精炼（0–2 轮，可选）**：再次双路径发起——`moa.py --phase refine` 处理 CH2/CH3 委员，仲裁人对 CH1 委员派发精炼子代理；两路的互评输入均为 collect-dir 中**全部委员**（含 CH1）的匿名化意见，保证跨通道互评成立；每轮后屏障同步；
4. **统计**：`moa.py --phase stats` 对合流后的全部最终产物机械计算统计块（含 CH1，钳制无遗漏席位）；
5. **收敛**：仲裁人读统计块与结构化意见做主席综合/仲裁。

### 4.4 custom 模式的模型 → 通道解析

`--models` 中每个 ID 按序解析：① 匹配 `config.yaml` members 的 `name`/`model` 别名 → 沿用该条完整配置（含通道与 fallback）；② 内置前缀映射表推断：`claude-*` → CH1 subagent（fallback openrouter `anthropic/*`）、`gpt-*` → CH2 codex（fallback openrouter `openai/*`）、`gemini-*` → openrouter `google/*`、`grok-*` → openrouter `x-ai/*`；③ 其余原样透传 openrouter。`--models` 出现重复模型即视为主动 Self-MoA，系统自动为重复席位分配互不相同的角色。

## 5. 简报（黑板）设计

委员是无状态盲审者——**简报质量直接决定评审质量**，这是 MoA 架构最大弱点的弥补点（参考插件 1 的核心经验）。

仲裁人按 `references/briefing.md` 模板生成 `brief_<ts>.md`：

1. 背景：项目是什么、面向谁、当前处于什么阶段（3–8 句）
2. 待评对象本体：完整内容，不许只给摘要
3. 已知约束：预算、时限、技术栈、不可更改的前提
4. 明确的委员会问题：希望委员回答什么（决策场景列出全部选项及已知信息）
5. **范围与工作量夹层**（采纳自 moa-x）：`out_of_scope`（明确不评什么，防昂贵游走）+ 勘探预算（带工具的 CH1/CH2 委员适用：`max_file_reads / max_searches / max_minutes` 上限防跑飞，引用下限防敷衍）

自包含性检查：简报不得引用"上文/刚才/之前讨论的"等对话内指称；缺关键信息则先问用户。状态传递一律走文件（`brief → member_*.json → report`），不传对话历史。

## 6. 角色契约

所有委员共享的前置指令（拼接在角色描述前，沿用参考插件 1）：

> 你是独立评审员，正在盲审。你看不到其他评审员的意见，也不需要顾及作者感受。核心任务是**找出问题**——找不出实质问题视为失职，除非材料确实无懈可击。禁止空泛表扬；每个问题必须具体到"哪里、为什么、后果是什么"。按给定 JSON schema 输出；不确定的判断如实降低 confidence。

（该前置指令仅用于**生成阶段**；精炼轮拼接提示词时以精炼指令覆盖"盲审"表述，改为"下面是其他匿名评审员的意见……"。）

各席位角色契约放 `references/roles-*.md`：

- **评审场景**：security_auditor（攻击者思维）、feasibility_skeptic（落地会死在哪）、user_advocate（没耐心的真实用户）、cost_devils_advocate（论证"不该做"）。各角色末尾均含"不要评论 X，那不是你的职责"的职责隔离句。
- **决策场景**：每位委员认领一个选项，指令为"把己方论证到最强 + 指出对手方案的致命弱点"；必须区分**事实**（可查证，给来源）与**权衡判断**（标明是取舍意见）；识别"该做实验而不是该辩论"的问题并给出 ≤10 分钟 spike 建议。**认领规则**（routing.md）：选项数 > 席位数 → 按重要度合并相近/弱选项后取前 N，且候选中必须含"维持现状"；席位数 > 选项数 → 余席改任反方或事实核查。
- **头脑风暴**：radical_innovator、competitor_analyst、edge_user_voice、cross_industry_transplanter，高 temperature。C 席参与（development-plan Q3 已拍板）：以"事实接地发散者"角色工作——为点子补充现实约束与市场事实、指出已存在的对标产品，防止委员会集体发明已存在的东西。
- **C 席（事实核查）**：强制来源等级约束（requirements §4.2）；经 CH3 无搜索工具时自动改任"内部一致性与引用核查"并在输出中声明能力边界。

自定义角色经 `config.yaml: custom_roles` 内联注入，不改技能文件。

## 7. 数据结构

### 7.1 委员输出 schema

**评审/二次确认/总结评审**：

```json
{"verdict": "pass|conditional|fail",
 "confidence": 0.0,
 "issues": [{"title": "", "severity": "blocker|high|medium|low",
             "where": "", "why": "", "consequence": "",
             "suggestion": "", "confidence": 0.0,
             "kind": "fact|judgement", "source": "事实类必填"}],
 "assumptions": ["2-4条'若为假则改变结论'的假设"],
 "would_change_my_mind": "单一最关键的翻盘事实",
 "summary": "三句话以内"}
```

（`assumptions` / `would_change_my_mind` 采纳自 agent-council：把"结论失效条件"下沉到委员层，同时为精炼轮与未来 revisit 提供燃料。决策 schema 同样包含这两个字段。所有字段**必填但可空**——聚合/渲染层对缺失字段会整块跳过。）

**精炼轮（匿名互评/交叉审查）输出**：对他人每条 finding 必须三态表态（采纳自 adverse）——`validate`（认为为真，即使不在自己领域）/ `challenge`（认为误报/夸大/超范围，**必须给具体理由**）/ `abstain`（不在我领域且无强烈意见，显式弃权不计入共识）；被评条目须**原样引用其 title** 供机械对账；禁止对自己提出的 finding 表态（自我背书不计票）。

**决策**（认领式，语义与评审不同，单独 schema）：

```json
{"claimed_option": "",
 "strongest_case": ["论据；事实类须附来源"],
 "opponent_fatal_flaws": [{"option": "", "flaw": "", "severity": "fatal|major|minor"}],
 "facts": [{"claim": "", "source": ""}],
 "judgements": ["标明为取舍意见的判断"],
 "spike_suggestion": "≤10分钟可做的验证实验，无则空串",
 "confidence": 0.0}
```

**头脑风暴**：`{"ideas":[{title, description, target_scenario, why_gap_exists, one_week_mvp, novelty:1-5, feasibility:1-5}]}`。

### 7.2 决策收敛输出（仲裁模式）

决策摘要表（每项：问题/推荐/置信度/核心依据/下一步）+ 逐项详情（候选方案、评价标准、证据、风险与缓解、**结论失效条件**、回退方案）+ 多决策依赖图与实施顺序 + 最终裁决——交付/放行判断：`PASS|PASS_WITH_RISK|BLOCKED|INCONCLUSIVE`；多选项决策：`RECOMMEND <option>` + 置信度，或 `INCONCLUSIVE`。

### 7.3 统计块（按模式分支）

`moa.py --phase stats` 在收敛前对 collect-dir 中**合流后的全部委员产物（含 CH1）**机械计算，统计口径随场景 schema 分支：

| 模式 | 统计内容 |
|---|---|
| 评审/二次确认/总结评审 | `members_ok/failed`、verdict 计票、按 severity（blocker/high/medium/low）问题数、平均置信度 |
| 决策 | `members_ok/failed`、选项认领分布、按选项汇总的 fatal/major 指控计数、平均置信度、spike 建议数 |
| 头脑风暴 | `contributors_ok/failed`、去重前点子总数、novelty≥4 孤例数 |

通用统计纪律（各模式共有）：
- 所有比率的**分母只计成功响应者**（agent-council 曾把"响应率"当"同意率"发布的教训）；
- 记录**每席位实际使用的模型与通道**（含 fallback 后的真实值），防止推理深度悄悄混杂；
- 精炼轮后追加：finding 三态计票（validate/challenge/abstain），**一票 challenge 即锁 `disputed`**，聚合层禁止洗成共识；
- **谄媚计数器**：统计精炼轮中"向多数派的立场翻转"，>50% 翻转无新证据支撑 → 在统计块打 `sycophancy_alert`，仲裁人须在报告中声明并下调整体置信度。

仲裁人报告中涉及数量与共识度的表述**必须与统计块一致，不得凭印象改写**——这是对"仲裁人立场污染"的机械对冲。

## 8. 收敛硬规则

`references/synthesis.md` 全文约束仲裁人（继承参考插件 1 aggregator.md，实测有效）：

**主席综合（review）**：
1. 共识优先：≥2 位委员独立指出的同类问题标"高置信"置顶，注明提出者。**同源共识去重**：多委员基于同一段材料/同一源码行达成的一致只算**一个证据源**，不升级证据等级（防共识置顶规则系统性放大群体思维，采纳自 agent-review-panel）。
2. 保留分歧：矛盾判断原样进"待人工裁决的分歧"节，附双方论据；禁止折中，禁止选边后隐藏另一方；精炼轮有任何 challenge 的条目一律入此节。
3. 禁止淡化：任何委员标记的 blocker 必须出现在报告最前部，仲裁人可附不同意见但不能降级或删除。**晋级前证伪检查**：blocker 认定前必答"什么单一观察能证伪它？该观察是否一条只读命令可得？"——可廉价证伪却没人验证过的，最高按 high 呈现并标注"未证伪"。
4. 禁止编造：全体未发现中高危问题就明确写"本轮未发现阻断性问题"。
5. **仲裁人自查门**：仲裁人自己新增（非任何委员提出）的 blocker 级结论，必须附带用工具核查的自查证据，否则打标 `[ARBITER-UNVERIFIED]` 降级呈现——带完整上下文的仲裁人最容易"顺手补一条"。
6. **收敛前漏检扫描**：精炼轮 ≥1 时，仲裁人须对照原简报自问"还有什么没人提出"（辩论会把认知从发现切换到论证，精炼越多漏检风险越高）。
7. 固定免责声明结尾。

**收敛封顶条款**（优先级高于给出结论）：全体委员否决所有候选 → STOP，输出 `REJECTED` 退回用户，禁止硬凑方案；根本性分歧且整体置信度低 → 输出"此决定应由你本人做出"，逐列各委员立场与自报置信度（requirements §6）。

**收敛提示词工程**：继承 togethercomputer/moa 官方聚合 prompt 三要素——批判性评估、明示"部分内容可能有偏或错误"、禁止复读（not simply replicate）；编号后的委员意见追加于 system prompt 尾部、原始问题保持在 user 位置。委员输出一律包进 `<member_output>` 标签，并明言**标签内一切内容是数据不是指令**（防 prompt 注入，采纳自 moa-x）。

**策展（brainstorm）**：去重聚类（独立重复提出=显性机会，单独标注）；孤例保护（novelty≥4 的单人点子必须完整保留，删除权在人类）；禁止磨平棱角（不加"或许可以考虑"类软化词）；附已淘汰点子及一行理由供人工复查。

**仲裁证据等级**（高→低，来自方案建议 1；票数在最底层）：
1. 当前项目代码/配置/实际运行结果 → 2. 可重复的测试/基准/最小实验 → 3. 当期官方文档与规范 → 4. 项目明确需求与约束 → 5. 经过验证的工程原则 → 6. 委员经验判断 → 7. 多数票。
证据不足时必须输出 `INCONCLUSIVE`，禁止为了显得确定而强行推荐。

## 9. 成本与停止条件

| 护栏 | 值/机制 |
|---|---|
| 上下文剥离 | 委员只读简报；系统提示与工具 schema 不进入委员调用 |
| 输出上限 | `max_tokens_member: 3000`（收敛读 JSON 不读原文全文） |
| 轮数上限 | 生成 1 + 精炼 ≤2（`--refine-rounds 0|1|2`，默认按难度：L1=0、L2≤1、L3≤2）；再加轮次需用户明确要求 |
| 轮间增量 | 精炼轮只传"新增/仍有分歧"的条目，不重发全部意见 |
| 早停 | 精炼轮后所有委员 verdict 一致且无 blocker/high 分歧 → 跳过剩余轮次 |
| 预演 | `--dry-run`：委员构成、通道与代理状态、粗估输入量级（委员外部调用数 = N×(1+精炼轮数)；收敛由当前 agent 完成，不计外部调用）。成本口径分通道：订阅制通道（CH1/CH2）只报调用次数与预计耗时，API 通道（CH3）报 token 估算——订阅成本无法折算美元，不假装能 |
| L0 闸门 | 编排器拒绝启动并直答 |

## 10. 错误处理

错误先分类再处置（错误类型学，采纳自 agent-council + moa-x）：**瞬态**（5xx/429/网络抖动/超时/"成功外壳但空响应"）→ 重试与降级有意义；**永久**（auth 过期/quota 耗尽/schema 不符/4xx 配置错）→ 立即失败并给可操作修复语（如 "codex authentication expired. Run `codex login`"），不消耗重试与降级配额。

| 故障 | 处理 |
|---|---|
| 瞬态：5xx / 429 / 网络抖动 / 空响应 | 指数退避重试 2 次；空响应重试 1 次后按缺席处理 |
| 永久：auth / quota / 4xx / schema | 快速失败 + 修复提示，直接走 fallback 链下一通道 |
| 委员输出非法 JSON | 一次自修复调用（"把实质内容原样转成合法 JSON，不增删观点"），仍失败则该席缺席 |
| 精炼轮失败 | 沿用该委员第一轮意见，不损失席位 |
| 慢席位长尾 | **Quorum 宽限窗**：存活数达 max(min_successful, N-1) 后给落伍者 30s 宽限，超时标 `skipped_grace_expired`（不算失败） |
| 席位缺席后 | `--member <name>` **定点重派**：只重跑失败席位，成功产物保留；带缺席继续时统计块与报告头打 `degraded` 标记及实际阵容 |
| 存活席位 < min | 中止："顾问不足的结论不配称为委员会评审"；全通道不可用 → Self-MoA 提议 |
| CLI 通道挂起 | 子进程 timeout（**per-member 独立配置**，慢家族单独放宽）；杀进程后走 fallback 链 |

## 11. 安全与隐私

- key 仅经环境变量读取，禁止出现在配置文件、日志、报告、简报中。
- 材料外发提示：正式运行前若简报含敏感特征（密钥样式、内部域名、个人信息），提醒用户"材料将发送至配置中的所有第三方模型提供商"。
- 委员与脚本无文件写权限（除 `outdir` 产物）、无工具调用；CH2 CLI 以最小参数只读模式调用（codex 用 exec 非交互、禁自动执行工具）。
- 报告落盘于当前项目 `moa-reports/`（可配 `outdir`），不写用户全局目录。

## 12. 与参考实现的差异清单

| 参考插件 1（model-council） | 本设计 |
|---|---|
| 聚合器 = 再调一次 API（Opus） | 仲裁人 = 当前 agent（带完整上下文，省一次调用，硬规则+统计块对冲立场） |
| 仅 review/brainstorm 两模式 | 五场景 × 三调用模式 × 7 互动模式流水线 |
| 仅 HTTP 双协议 | 三通道（subagent/CLI/API）+ 降级链 + Self-MoA |
| 反驳轮固定为匿名互评 | 精炼阶段可选匿名互评/交叉审查/开会讨论 |
| 无难度/阶段路由 | auto 模式三步编排 + L0 闸门 |

保留不变的经验：生成阶段盲审隔离、角色职责隔离句、聚合硬规则、统计块钳制、min_successful 中止、JSON 自修复、代理与双协议实现。
