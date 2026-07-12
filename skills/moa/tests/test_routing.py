"""auto 模式场景→流水线路由回归集(requirements §12.2 / references/routing.md)。

**这是近似回归门,不是真正的路由器。** 真正的 auto 路由是 Claude(仲裁人)读简报后
按 routing.md 三步(场景×难度×阶段)做语义判断;此处的 `classify_scenario` 只把
routing.md「Step 1 场景识别」+「场景→流水线映射」两张表里**明文写出的信号词**编码成
关键词启发式,守住"这些信号能把五类场景映到正确流水线"这条不变量。

覆盖 requirements §12.2 前半:auto 对 §6.1 五类场景选出正确默认流水线,每场景 ≥2 条固定正例。
(后半「L0 双闸门 ≥10 正/≥10 负、通过线 ≥9/10」由 test_triggers.py 覆盖。)

改了 routing.md 的信号词或流水线映射后,回来同步 SCENARIO_PIPELINE 与用例。
"""

# routing.md「场景→流水线映射」表(生成 / 精炼[L2+] / 收敛),五类场景各一行。
# 二次确认与总结评审 mode 都落 review,但生成/精炼阶段不同,故按场景键区分。
SCENARIO_PIPELINE = {
    "review":         ("review",     "role_play",          "peer_review",     "chair_synthesis"),
    "decide":         ("decide",     "role_play_claim",    "cross_exam",      "arbitration"),
    "brainstorm":     ("brainstorm", "role_play_diverge",  "none",            "curation"),
    "reconfirm":      ("review",     "independent",        "none",            "chair_synthesis"),
    "summary_review": ("review",     "independent_or_role", "peer_review_L3", "chair_synthesis"),
}

# routing.md Step 1 明文信号词(顺序即判定优先级:更具体的复合意图先判)
_SUMMARY_REVIEW = ("对上面的总结", "对以上的总结", "总结做出评审", "总结做出建议", "对总结")
_RECONFIRM = ("二次确认", "再确认", "复核一下", "再核实", "对上面的分析确认", "对上面的结果确认")
_RECONFIRM_CTX = ("上面", "以上", "刚才", "上述", "前面")  # 「确认」类须带指代上文
_BRAINSTORM = ("头脑风暴", "brainstorm", "点子", "发散", "差异化", "有哪些方向", "想几个")
_DECIDE = ("选型", "还是", "哪个", "决策", "取舍", "二选一", "三选一", "decide", "选哪")
_REVIEW = ("评审", "审查", "审核", "把关", "review", "评估")


def classify_scenario(text: str):
    """routing.md Step 1 信号词的关键词近似 → 场景键(命不中返回 None)。"""
    low = text.lower()

    def has(words):
        return any(w in text or w in low for w in words)

    if has(_SUMMARY_REVIEW):
        return "summary_review"
    if has(_RECONFIRM) or ("确认" in text and has(_RECONFIRM_CTX)):
        return "reconfirm"
    if has(_BRAINSTORM):
        return "brainstorm"
    if has(_DECIDE):
        return "decide"
    if has(_REVIEW):
        return "review"
    return None


def pipeline_for(text: str):
    """场景 → (mode, 生成, 精炼, 收敛);命不中场景返回 None。"""
    scen = classify_scenario(text)
    return SCENARIO_PIPELINE.get(scen) if scen else None


# (utterance, expected_scenario) —— 五类场景各 ≥2 条固定正例
CASES = [
    # 评审/审查/审核
    ("帮我评审这份微服务拆分方案", "review"),
    ("review 一下这段登录代码有没有问题", "review"),
    ("审核这份数据库迁移设计", "review"),
    # 决策/推荐
    ("PostgreSQL 还是 MongoDB,帮我决策主存储选型", "decide"),
    ("这三个缓存方案选哪个", "decide"),
    # 头脑风暴
    ("头脑风暴一下产品差异化方向", "brainstorm"),
    ("帮我想几个降低新人上手成本的点子", "brainstorm"),
    # 二次确认
    ("对上面的分析做二次确认", "reconfirm"),
    ("刚才这个结论,帮我再确认一下", "reconfirm"),
    # 总结评审
    ("对上面的总结做出评审", "summary_review"),
    ("对上面的总结做出建议", "summary_review"),
]

# 已知边界:多场景复合意图,routing.md 规定拆两次调用(先 review 后 brainstorm);
# 关键词启发式只能挑一个场景,如实登记为边界(真实语义路由会拆分)。
KNOWN_BOUNDARY_COMPOSITE = "评审现有方案并头脑风暴几个替代方向"


def test_five_scenarios_each_have_at_least_two_cases():
    from collections import Counter
    c = Counter(exp for _, exp in CASES)
    assert set(c) == set(SCENARIO_PIPELINE), f"场景覆盖不全: {set(SCENARIO_PIPELINE) - set(c)}"
    assert all(n >= 2 for n in c.values()), f"每场景须 ≥2 例: {dict(c)}"


def test_each_case_routes_to_correct_pipeline():
    misses = [(t, exp, classify_scenario(t)) for t, exp in CASES if classify_scenario(t) != exp]
    assert not misses, f"场景误判: {misses}"


def test_pipeline_tuple_shape_stable():
    # 每条流水线 = (mode, 生成, 精炼, 收敛) 四元组,防映射表被改残
    for scen, pipe in SCENARIO_PIPELINE.items():
        assert len(pipe) == 4, f"{scen} 流水线元组不是四元组: {pipe}"
    # 决策必是 认领→交叉审查→仲裁;头脑风暴无精炼
    assert SCENARIO_PIPELINE["decide"][2] == "cross_exam"
    assert SCENARIO_PIPELINE["decide"][3] == "arbitration"
    assert SCENARIO_PIPELINE["brainstorm"][2] == "none"
    assert SCENARIO_PIPELINE["brainstorm"][3] == "curation"


def test_composite_intent_is_registered_boundary():
    # 复合意图:routing.md 规定拆两次调用(先 review 后 brainstorm)。关键词启发式无法拆分,
    # 只挑到单一场景(此处命中 brainstorm),与"应先 review"的意图分歧——如实登记为边界。
    # 真实语义路由会识别复合并拆两次;此断言只锁"启发式退化成单场景"这一已知近似事实。
    assert classify_scenario(KNOWN_BOUNDARY_COMPOSITE) in ("review", "brainstorm")
