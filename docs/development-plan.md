# MoA Skill 开发计划

> 版本 v1.0 · 2026-07-11 · 状态：**M1 ✅ · M2 ✅ · M3 ✅（均经真实 API E2E 验证）· M4 收尾中**
> 上游：[requirements.md](requirements.md)、[design.md](design.md)、[reference-adoption.md](reference-adoption.md)（v1.0 采纳 16 项已并入各里程碑；实现前必读其 §4 工程红线）

## 0. 实现状态（2026-07-11 回填）

| 里程碑 | 状态 | 验证证据 |
|---|---|---|
| M1 核心委员会 | ✅ 完成 | 49 项 pytest 全绿；真实 API 评审（2 席）产出结构化意见 + stats |
| M2 三通道与智能路由 | ✅ 完成 | codex CLI（CH2）+ API（CH3）真实成功；拔 key 验证 fallback；auto 路由决策表落地 |
| M3 场景与互动模式 | ✅ 完成 | 评审/决策/头脑风暴三场景；精炼轮真实 E2E（三态契约、谄媚计数、早停均产出真实数字） |
| M4 打磨与加固 | ✅ 基本完成（v1.0） | 已完成：**成本实测**（4.79×，见 §0.1）、**触发用例集**（正负各 13 例，启发式 92%）、**README + troubleshooting**、**docs 状态回填**、**usage 捕捉**、**敏感材料外发告警 + `leak-check` 密钥泄漏静态自查**（脱敏，命中非零退出）、**`.claude-plugin/plugin.json` 分发清单 + 打包边界**。剩余可选：真实 4 席全配 E2E、v1.1 backlog（§5.1） |

### 0.1 成本实测结果（M4）
基线 = 2030 tok/席·轮；实测 2 席 + 1 精炼轮 = **4.79×**；默认 config 因 A/B 走订阅通道，用户实付倍数 ≈ 4.79× ≤ 7× 目标 ✓。全 CH3 四席 + 1 精炼轮外推 ≈ 9.6× 超标（降本手段见 [requirements §11](requirements.md#11-非功能需求) 与 [moa-reports/cost-m4/COST-NOTE.md](../moa-reports/cost-m4/COST-NOTE.md)）。

---

## 1. 总体策略

- **先 Skill 后 Plugin**：核心工作流以单个 Skill 交付并在真实使用中稳定后，再封装 plugin.json 分发（方案建议 1 的结论，也符合官方定位）。
- **每个里程碑独立可用**：M1 完成即是一个能干活的多模型评审工具；后续里程碑只做增量。
- **确定性逻辑进脚本、判断逻辑进提示词**：通道调度/重试/统计/成本估算全在 `moa.py` 用测试覆盖；路由与收敛质量靠角色契约与硬规则文本，用真实用例验证。
- 复用底座：参考插件 1 的 `council.py` 约 350 行已实现 HTTP 双协议、代理、重试、JSON 修复、min_successful、dry-run——M1 以它为起点改造，不从零写。

## 2. 里程碑

### M1 核心委员会（最小可用）

范围：
1. 目录骨架（design.md §2）+ SKILL.md 初版（触发词、full/custom 两模式、简报流程）
2. `moa.py`：CH3 API 通道（openai/openrouter 双协议、代理、重试、JSON 修复、min_successful 动态阈值、dry-run），从 `council.py` 改造：members/seat/role 结构、**`--phase generate|refine|stats` 子命令骨架**（refine 逻辑 M3 填充，stats M1 实现）、`--member` 子集运行、`--collect-dir` 产物约定（design.md §4.3 两阶段协议）
3. 评审基础流水线：角色扮演盲审 → 主席综合（当前 agent 按 synthesis.md 硬规则，含 v1.0 采纳的证伪检查/自查门/同源去重/封顶条款/防注入/聚合 prompt 骨架）
4. `references/`：roles-review.md、synthesis.md、briefing.md（简报含 out_of_scope + 勘探预算夹层）
5. 委员 schema 落地 assumptions / would_change_my_mind 字段（必填可空）
6. config.example.yaml（四席顶配默认值，per-member 独立 timeout）

验收：`/moa full` 对一份真实设计文档、`/moa custom`（2 委员指定模型）各产出双格式报告；dry-run 正确；故障注入（超时/坏 JSON/全挂）行为正确；构造分歧用例验证硬规则（分歧保留、blocker 不降级）；L1 单委员配置不被 min_successful 误杀。

### M2 三通道与智能路由

范围：
1. CH1 子代理通道（Task 派发 + 产物合流时序，design.md §4.3）
2. CH2 CLI 通道（codex 探测与子进程调用；prompt 走 stdin/临时文件、`--sandbox read-only`、空响应重试——工程红线 1–3）
2b. 错误类型学（瞬态/永久二分 + 修复提示）、Quorum 宽限窗、`--member` 定点重派、degraded 标记（design.md §10）
3. auto 模式：routing.md 决策表 + 三步编排 + L0 闸门 + 编排结果一句话公示
4. 关键词触发（T2）与主 agent 自调（T3）写入 SKILL.md description
5. **上下文简报构建 + 独立回答流水线**（二次确认/总结评审场景）——T2 原生触发词"对上面的分析/总结做出建议"依赖此项，必须与 T2 同里程碑交付
6. Self-MoA 两路径：主动（`--self-moa` / `--models` 重复模型 + 差异化角色自动分配）与零通道兜底，均含显式声明

验收：三通道各有真实成功案例；拔掉 key/卸载 CLI 验证降级链与阵容声明；L0 闸门用例集（≥10 正例/≥10 负例）通过线各 ≥9/10；五类场景样例路由到正确流水线；"对上面的总结做出建议"端到端可用；Self-MoA 两路径端到端可跑。

### M3 场景与互动模式扩展

范围：
1. 决策场景：roles-decide.md（认领+对抗论证+认领规则）、决策委员 schema、交叉审查精炼、仲裁收敛（对比矩阵/RECOMMEND 裁决/失效条件/多决策依赖）
2. 头脑风暴场景：roles-brainstorm.md、策展硬规则
3. 匿名互评精炼轮（`--phase refine`，`--refine-rounds ≥1`，含 CH1 跨通道互评）：validate/challenge/abstain 三态契约、title 精确对账、自我背书排除、一票 challenge 锁 disputed、谄媚计数器、轮间增量传递、早停、收敛前漏检扫描
4. 开会讨论模式（仅 L3 且用户显式要求）

验收：requirements §12 第 7 条三组真实验证（文档评审/多选项决策/头脑风暴）的报告按**硬规则检查清单**逐项通过（分歧节保留、blocker 置顶、免责声明存在、报告数字与统计块一致、决策含失效条件与依赖顺序、孤例点子未被删除）；互评轮成本增量与早停可观测。

### M4 打磨与加固

范围：
1. 敏感材料提示、key 泄漏静态自查、报告 outdir 规范
2. 成本实测与简报/提示词精简（对照 requirements §11 "L2 默认配置 ≤ 基线 7 倍"目标，回填实测倍数）
3. SKILL.md description 打磨（触发用例集：≥10 正例 / ≥10 负例，通过线各 ≥9/10）
4. 使用文档（README：安装、env 配置、三模式示例）与 troubleshooting
5. （可选）封装 .claude-plugin/plugin.json

验收：requirements §12 全部 8 条通过；SKILL.md ≤500 行；新机器按 README 从零跑通 `/moa full --dry-run`。

## 3. 测试计划

| 层 | 内容 | 方式 |
|---|---|---|
| 单元 | 配置解析/席位-模型映射/代理判定(no_proxy边界)/JSON 修复/统计块/成本估算 | pytest，无网络 |
| 集成 | 双协议请求构造、重试退避、min_successful 中止、fallback 链、CLI 子进程超时 | 本地 mock HTTP 端点 + 假 CLI 脚本 |
| 提示词回归 | 硬规则用例集：含分歧材料、全绿材料（防编造）、blocker 材料（防降级）、孤例点子（防删除） | 固定用例 + 二值硬规则检查清单（每用例逐项判定，无主观评分），每次改 roles/synthesis 后重跑 |
| E2E | 三通道真调用 ×（评审/决策/头脑风暴） | 真实 key，产物人工复核 |
| 安全 | 日志/报告/简报 grep key 样式；敏感提示触发 | 脚本化检查 |

## 4. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 模型 ID 迭代失效 | 委员调用 404 | ID 仅存配置层；dry-run 提示核对；fallback 链兜底 |
| CH2 CLI 输出格式不稳（codex 非交互输出混杂） | JSON 提取失败 | stdout JSON 抽取 + 自修复；不稳定则该席走 API fallback |
| 仲裁人立场污染（自己综合自己召集的评审） | 结论偏向主 agent 原有观点 | 统计块钳制 + 硬规则禁折中/禁降级 + 原始意见附录供人工复核 |
| "假讨论"（互评轮无信息增量） | 白烧一轮 token | 互评指令要求逐条"同意/反驳/合并"并给理由；早停机制 |
| 关键词误触发 | 简单问题烧委员会 | L0 闸门 + 触发负例写入 description + dry-run 默认建议 |
| 成本失控 | 用户账单不可预期 | max_tokens/轮数上限/增量传递/成本实测（M4） |
| 敏感材料外发 | 隐私泄漏 | 外发提示 + 用户确认；纯本地场景可 custom 全 CH1/CH2 |

## 5. 交付清单（v1.0）

- `skills/moa/`：SKILL.md、references/ ×6、scripts/moa.py、assets/config.example.yaml
- 测试：单元+集成套件、提示词回归用例集、E2E 记录
- 文档：README（安装/配置/示例）、troubleshooting
- 三组真实验证报告样例（脱敏后作为 examples）

## 5.1 v1.1 Backlog（采纳决议中的候选项，按价值排序）

1. **决策生命周期三件套**：outcome 结果记录（积累各模型擅长域校准数据）→ revisit 带新上下文重评（需 context snapshot 一并实现）→ nudge 定点纠偏——与"结论失效条件"衔接：失效条件触发即提示 revisit
2. agreement 量化分数早停（须伴随分维度明细，防单标量和稀泥）
3. CI 退出码契约（headless 模式接 CI 质量门禁）
4. 自评估基准框架（N 道题 × expected-considerations 覆盖率，作为 skill 自身回归指标）
5. HTML 自包含报告（互评矩阵 + 时间线；渲染失败不影响运行成功）
6. 其余候选见 [reference-adoption.md §2](reference-adoption.md)

## 6. 已拍板问题（2026-07-11 用户确认）

| # | 问题 | 结论 |
|---|---|---|
| Q1 | CH1 子代理能否指定非会话默认模型 | **可以**——Agent 工具支持 model 参数指定非会话默认模型；fallback 链保留作故障兜底 |
| Q2 | codex CLI 非交互调用的确切命令形态与输出截取 | 本机为最新版；**M2 实现时以当期 codex 官方文档为准**定型命令形态 |
| Q3 | 头脑风暴场景 C 席（事实核查）是否参与 | **参与**——以"事实接地"视角发散：为点子补充现实约束、指出已存在的对标产品（见 design.md §6） |
| Q4 | 报告默认落盘目录名 | **`moa-reports/`**（用户可见、可提交） |
