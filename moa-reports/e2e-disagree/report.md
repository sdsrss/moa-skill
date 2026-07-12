# 主席综合报告:内部报表工具的用户密码存储方案

> 仲裁人按 `references/synthesis.md` review 硬规则合成。委员会 2 席(安全审计员 sec-a / 务实交付派 shipper-b),1 轮匿名互评。数量与共识度表述与 `stats.json` / `stats.r1.json` 一致。

## 结论摘要

**不通过(存在 1 个上线前必修 blocker)** — 综合置信度中高。

- 生成轮 verdict:`fail` 1 票(sec-a)/ `conditional` 1 票(shipper-b);两席均未给 `pass`。
- 问题分布(`stats.json`):blocker 1 · high 3 · medium 2 · low 0。
- 核心分歧不在"问题是否存在"(两席都认可无盐+快哈希存密码有风险),而在**同一问题的 severity 与紧迫度**:sec-a 判 blocker(上线前必修),shipper-b 判 high 并主张"下个迭代"。按硬规则,该 blocker 不被降级(见下)。

## 高置信问题(按 severity)

### [BLOCKER] 密码无盐 + 快哈希(SHA-256)存储 — 上线前必修
- **来源**:sec-a 标 blocker(conf 0.9,fact);shipper-b 以同一事实标 high(conf 0.9,fact)。**两席独立认可问题为真**(精炼轮 sec-a `validate` 了 shipper-b 的同条目)。
- **失效路径**:`sha256(password)` 无盐 → 相同密码哈希相同,可直接命中彩虹表/预计算;SHA-256 是快哈希,GPU 每秒可试数十亿次,离线暴破可行。攻击面并非假想:简报载明 **2 名 DBA 有库读权限**,任一 DBA 账户被钓/内鬼即可拿到全量哈希做离线破解——"内网/可信"不消除这条离线面。
- **晋级前证伪检查(硬规则 3)**:(a) 能证伪它的单一观察 = "该库哈希永不可能被任何内部/外部主体读到";(b) 这**无法**用一条只读命令廉价证成(2 DBA 读权限已构成现实读取面)。故 blocker 认定成立,不打 `未证伪` 标。
- **修复**:换 bcrypt/scrypt/argon2(自带慢哈希 + 每用户盐);迁移可平滑(登录成功时按新算法重算入库)。这是 40 人规模一次性的小改动,不构成"过度设计"。

### [HIGH] 其余安全条目(共 3 条 high,见 stats)
- SHA-256 作为唯一密码哈希算法(与上条同根;sec-a high / shipper-b medium)。
- 数据库读权限过宽(2 DBA 直连读)——放大上面 blocker 的离线攻击面(sec-a high / shipper-b medium)。

## 待人工裁决的分歧(禁折中,双方论据原样保留)

精炼轮 `stance_tally`:validate 1 · **challenge 5** · abstain 0;`disputed_titles` 锁定 5 条(含本 blocker)。`sycophancy_alert: false`(无一方无理由倒向多数)。

**分歧点:无盐+快哈希该判 blocker(上线前必修)还是 high(下个迭代)?**

- **sec-a(判 blocker,主张必修)**:无盐使彩虹表可用、快哈希使暴破可行;2 DBA 读权限构成现实离线面;密码一旦被破,40 个账户及其可能的口令复用全部沦陷。属"库一泄即全崩"的不可逆后果,不应带病上线。
- **shipper-b(challenge,主张降 high + 下迭代)**:仅限内网、40 名可信用户、本周内必须上线;换慢哈希会延后上线;当前已限制为后端服务 + 2 DBA 访问,风险"相对可控",可在下个迭代补盐+换算法。**(注:shipper-b 自己也承认该问题为真,只调 severity/紧迫度,未否认存在。)**

**仲裁人裁定**:按硬规则 3,委员标记的 blocker **不降级**——本报告维持其为上线前必修项。shipper-b 的时间压力论属**风险接受(risk acceptance)决策**,而非技术反驳:它不改变"库一泄即全量密码可破"这一事实,只是主张承担该风险换取工期。**该风险接受与否是人的决定,不是委员会能替你拍的**——若业务确认愿承担"内网库泄露=全账户密码泄露"的后果以赶本周工期,可显式签署接受后上线;否则按 blocker 修。仲裁人不做"折中判 high"的降级处理(那会把一个人的风险接受伪装成技术结论)。

## 各委员意见摘要

- **sec-a(security_auditor,verdict=fail,conf 0.9)**:无盐(blocker)+ 快哈希(high)+ DBA 读权限过宽(high);结论"必须上线前修复"。精炼轮 validate 了无盐条、challenge 了 SHA-256/DBA 两条的紧迫度(自身也认为这两条短期可接受)。
- **shipper-b(pragmatic_shipper,verdict=conditional,conf 0.8)**:同样认可无盐(high)+ SHA-256(medium)+ DBA(medium)三问题为真,但主张在内网低危 + 工期压力下降级并放下个迭代;精炼轮对 3 条 blocker/high 均 challenge。

## 免责声明

> 各模型训练数据存在重叠,全员未发现不代表不存在问题;本报告的分歧点需人工裁决。本轮为 2 席小委员会 + 便宜模型档的**流程 E2E 验证**产物,非对该密码方案的顶配安全结论。
