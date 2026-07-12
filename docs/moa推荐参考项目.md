

## 推荐项目

### 1. Agent Council——最适合方案决策

[yogirk/agent-council](https://github.com/yogirk/agent-council)

- 支持 Claude Code、Codex CLI、Gemini CLI。
- 多个 Agent 独立提出意见，再互相评审，最后由主席 Agent 综合。
- 支持架构决策、技术选型、代码审查、问题诊断。
- 提供历史决策、重新评议、纠偏和 HTML 报告。
- MIT 开源协议，约 87 Stars。
- 有 59 项测试、133 个断言。

最符合你提出的“遇到几条方案时直接丢进去，让它给出科学建议”。

------

### 2. MoA-X——最接近真正的 Coding MoA Skill

[drivelineresearch/moa-x](https://github.com/drivelineresearch/moa-x)

工作流：

```text
Scout 分析仓库
→ Codex、Claude、GLM 并行生成方案
→ Codex、Kimi 查看所有方案并精炼
→ Claude Opus 聚合最终实施计划
```

- 基于经典 Mixture-of-Agents 思想。
- 面向真实代码仓库生成实施方案。
- 支持 Claude Code Skill 和独立 Python 运行。
- Agent 阵容、模型、层数均可配置。
- 自动生成 HTML 执行报告。
- MIT 协议，约 25 Stars。
- 2026 年 7 月仍在更新。

这是最值得研究其代码架构的项目。

------

### 3. Agent Review Panel——评审与仲裁最完善

[wan-huiyan/agent-review-panel](https://github.com/wan-huiyan/agent-review-panel)

流程：

```text
收集上下文
→ 4～6 个 Agent 独立评审
→ 相互辩论
→ 验证问题
→ Judge 仲裁
→ 输出报告
```

- 可审查代码、架构方案、文档和配置。
- 自动选择安全、正确性、性能、可维护性等角色。
- 内置反群体思维机制。
- 输出 Markdown、完整过程记录和 HTML 报告。
- MIT 协议，约 26 Stars。
- 最新版本 v3.6.0，发布于 2026 年 6 月。

特别适合借鉴你的“审查 Agent + 仲裁 Agent”。

------

### 4. Adverse——代码审查工程化最好

[addyosmani/adverse](https://github.com/addyosmani/adverse)

- 同时提供 CLI 和 Claude Code Skill。
- Auditor、Adversary、Pragmatist 三个角色并行审查。
- 第二轮互相验证、反驳和补充发现。
- 支持 Claude、Codex、Gemini、Aider、Ollama。
- 可用于 CI 质量门禁。
- 输出 Markdown、JSON、HTML。
- MIT 协议，约 38 Stars。

不足是 Skill 模式默认属于“同一个模型扮演不同角色”，并非完全异构的跨模型 MoA。

------

### 5. Agent Tower Plugin——Council、辩论和迭代三种模式

[BayramAnnakov/agent-tower-plugin](https://github.com/BayramAnnakov/agent-tower-plugin)

提供三种工作模式：

- `council`：多个 Agent 独立回答、匿名互评、主席综合。
- `debate`：正反双方多轮辩论，Judge 裁决。
- `deliberate`：Producer 与 Reviewer 循环修改，直到达到共识阈值。

支持 Claude、Codex、Gemini；MIT 协议，约 30 Stars。

它的模式划分很清楚，特别适合借鉴“按问题类型智能选择工作流”。

------

### 6. Agent Council 全生命周期版

[andrewvaughan/agent-council](https://github.com/andrewvaughan/agent-council)

- 13 个专业 Agent。
- 6 个评审委员会。
- 9 个开发 Skill。
- 覆盖规划、构建、评审、热修复、安全审计和提交 PR。

主要 Skill：

```text
plan-feature
build-feature
build-api
review-code
hotfix
security-audit
submit-pr
```

这是现有项目中最接近你设想的：

```text
架构 → 编码 → 测试 → 审查 → 修复 → 交付
```

但项目约 8 Stars、没有正式 Release，目前更适合作为流程设计参考。

------

### 7. 原始 MoA 官方参考实现

[togethercomputer/MoA](https://github.com/togethercomputer/moa)

- Together AI 发布的经典 Mixture-of-Agents 实现。
- 多个模型分层生成和聚合。
- Apache-2.0 协议。
- 约 3,000 Stars。

它不是 Agent Skill，而是理解 MoA 分层、广播和聚合机制的理论与代码基础。

## 最终推荐顺序

如果你的目标是开发自己的 MoA 编程 Skill，建议优先研究：

1. `yogirk/agent-council`：跨 CLI 调度、决策和历史追踪。
2. `moa-x`：真正的分层 MoA 和广播精炼。
3. `agent-review-panel`：验证、争议处理和最终仲裁。
4. `adverse`：交叉审查、误报过滤和结构化报告。
5. `andrewvaughan/agent-council`：完整开发生命周期。

最理想的组合架构是：

```text
Agent Council 的跨模型调度
+ MoA-X 的多层提案与精炼
+ Adverse 的交叉质询
+ Review Panel 的证据验证与仲裁
+ 自己增加编码、真实测试、自动修复和回归循环
```

 
