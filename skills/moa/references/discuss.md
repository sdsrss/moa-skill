# 开会讨论模式(§6 阶段 5)——仲裁人编排协议

> **仅 L3 复杂争议且用户显式要求时启用。** 成本最高、有从众风险,是盲审原则的显式例外。
> 默认不进 auto,不做默认精炼轮替代品。轻量分歧用匿名互评(`refine`)即可。

## 何时用 / 不用
- **用**:高价值不可逆决策 + 委员在精炼轮后仍根本分歧 + 用户明确要"让它们真辩一轮"。
- **不用**:一般评审/决策(匿名互评或交叉审查更省、更抗从众)。开会讨论让后发者看到前发言,**从众风险最高**——所以配套了三重对冲(下)。

## 机制与现有精炼轮的区别
| | 匿名互评/交叉审查(refine) | 开会讨论(discuss) |
|---|---|---|
| 发言 | 并行、盲审、匿名 | **顺序**、后发可见前发、按**席位+角色**署名(仍不暴露模型) |
| 轮次 | 每轮一次性并行 | 多轮,每轮逐席顺序 |
| 编排 | `moa.py refine` 一条命令 | **仲裁人逐回合编排**,`moa.py` 提供 per-turn 助手 |

## 三重反从众对冲
> 第 1 条靠**你(仲裁人)**按轮次调 `--member` 发言序实现,`moa.py` 不强制、不校验;第 2、3 条由 `discuss-stats` 机械落实(`conformity_alerts` / `blind_vote_drift_pairs`)。别漏排轮转。
1. **发言顺序轮转**(仲裁人手工):每轮轮转发言序(第 1 轮 A→B→C,第 2 轮 B→C→A…),消除固定锚点/固定跟随者。
2. **每回合强制标注**(脚本机械):`position_changed` + `changed_by_new_argument` + `new_argument`。**无新论据却改立场 = 从众**,被 `discuss-stats` 机械计入 `conformity_alerts`。整轮全员 `new_argument` 空 = 假讨论(`pseudo_discussion_rounds`),末轮假讨论 → `early_stop_suggested`。
3. **收尾盲投**(脚本机械):讨论结束后每席**不看 transcript**独立复述最终立场(`discuss-blindvote`)。`blind_vote_drift_pairs` 给出(讨论终态 vs 盲投终态)配对——两者不一致 = 讨论诱发漂移的证据,交仲裁人判读。

## 编排步骤(仲裁人执行)

前提:已写好 `brief.md` + `config.yaml`(委员含 `role`;讨论型角色建议给"稳健折中"席以打破二元对立)。设发言序 `order`。

```bash
CFG=...; IN=...; D=moa-reports/<run>
# 每轮 r,按 rotate(order, r) 逐席:
#   CH2/CH3 席 → moa.py 直接派发并追加 transcript
python skills/moa/scripts/moa.py discuss-turn --mode <m> --config $CFG --input $IN \
  --collect-dir $D --member <seat_name> --round <r>
#   CH1 席 → 取精确 prompt,用 Agent 工具外派发子代理(禁工具/仅 JSON),把返回 JSON 存文件后 --inject 回填
python skills/moa/scripts/moa.py discuss-prompt --mode <m> --config $CFG --input $IN \
  --collect-dir $D --member <seat_name> --round <r>            # 打印 system/user
#   <dispatch subagent with that exact prompt, save JSON to turn.json>
python skills/moa/scripts/moa.py discuss-turn ... --member <seat_name> --round <r> --inject turn.json
```

- **顺序要点**:同一轮内必须**按发言序逐条追加**——后发席的 `discuss-turn`/`discuss-prompt` 会读到此前已追加的发言。不要并行同一轮(会丢掉"后发可见前发")。
- **CH1 一致性**:CH1 必须用 `discuss-prompt` 打印的**同一 prompt**外派发,保证与脚本派发的 CH2/CH3 看到相同 transcript。

收尾:
```bash
# 每席盲投(CH1 同样 discuss-prompt --blind 取词 → 外派发 → --inject)
python skills/moa/scripts/moa.py discuss-blindvote --mode <m> --config $CFG --input $IN \
  --collect-dir $D --member <seat_name> [--inject blind.json]
# 统计: 从众/假讨论/漂移/保留分歧
python skills/moa/scripts/moa.py discuss-stats --config $CFG --collect-dir $D
```

产物:`discussion.jsonl`(逐回合 transcript)、`blindvote_<seat>.json`、`discuss_stats.json`。

## 收敛(仲裁人)
读 `discussion.jsonl` + `discuss_stats.json`,按 `synthesis.md` 硬规则产出报告,并额外:
- `conformity_alert` 为真 → 在报告声明,**下调对"讨论达成的共识"的置信度**(共识可能是从众而非收敛)。
- `blind_vote_drift_pairs` 中讨论终态与盲投终态明显不一致的席 → 标注"该席立场在讨论中漂移,盲投显示其独立判断为 X",按盲投而非讨论终态计其真实立场。
- `dissent_preserved` 原样进"待人工裁决的分歧",禁折中、禁降级 blocker。
- `pseudo_discussion_rounds` 非空 → 说明"第 N 轮无信息增量",提示下次减少轮数。
