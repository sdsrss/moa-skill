# M4 成本实测记录（2026-07-11）

对照 requirements §11 目标：「L2 默认配置 token 成本 ≤ 单模型基线 7 倍」。

## 测量方法
- 脚本落地 `usage` 捕捉（`call_model` → `call_with_json_repair` → member JSON → `stats.token_usage` / `stats.r1.token_usage`），只累加 CH3 计费席，订阅席（CH1/CH2，usage=None）不计入。
- 材料：`brief.md`（一个 JWT 校验函数评审，603 字符，含 `algorithms=["none"]` 等植入缺陷）。
- 委员：2 席异构 CH3（`openai/gpt-4o-mini` + `openai/gpt-4.1-nano`，便宜模型，省 token）。
- 基线定义：**单席单轮 generate** = 一个同级模型对同一简报评审一次并产出同 schema，= 非 MoA 单模型评审的真实成本。

## 实测数字
| 轮 | prompt | completion | total | 计费席 |
|---|---|---|---|---|
| round 0 generate | 2137 | 1924 | **4061** | 2 |
| round 1 refine | 4189 | 1467 | **5656** | 2 |

- 基线（单席单轮）= 4061 / 2 = **2030 tokens**
- per-seat generate ≈ 2030；per-seat refine ≈ 2828 → **精炼轮 prompt 带前轮上下文，膨胀 1.39×**
- **实测倍数（2 席 + 1 精炼轮）= 9717 / 2030 = 4.79×**

## 外推到 4 席默认配置（结构外推，非实测）
| 配置 | total tokens | ×基线 |
|---|---|---|
| 4 席 + 0 精炼（L1 默认） | 8122 | **4.0×** |
| 4 席 + 1 精炼（L2 若开精炼） | 19434 | **9.6×** |
| 4 席 + 2 精炼（L3） | 30746 | 15.1× |

## 结论（对照 ≤7× 目标）
- **按用户实付 token 计**：默认 config 四席里 A（codex CLI）、B（子代理 Claude）走**订阅通道不计费**，只有 C/D 两席 CH3 计费。故用户实付倍数 ≈ 实测 2 席值 **4.79× ≤ 7× ✓**（含 1 精炼轮）。
- **按总算力计（假设四席全 CH3）**：4 席 + 1 精炼 ≈ **9.6×，超 7× 目标**。仅当用户把四席全配成 api 时相关。
- **可控项**：精炼轮把前轮全文塞进 prompt（1.39× 膨胀）是主要增量来源。降本手段：① L2 默认 refine=0（→4.0×）；② 四席保留 ≥2 个订阅通道席；③ v1.1 精炼 prompt 只传对方结论摘要而非全文。

**落库**：requirements §11 回填实测倍数与"全 CH3 四席 + 精炼超 7×"的警示。
