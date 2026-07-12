"""SKILL.md description 触发用例回归集(dev-plan M4 项 3 / requirements §12)。

**这是近似回归门,不是真正的路由器。** 真正的触发是 Claude 语义读取 SKILL.md
description 后的判断;此处的 `should_trigger` 只把 description 里**明文写出的规则**
(触发词表 + L0 闸门:算术/事实检索不启动)编码成关键词启发式,用来守住"这些规则能把
正负例分开"这条不变量。启发式与真实语义判断会在边界处分歧——用例集里各留 1 个已知
边界失配(见 KNOWN_BOUNDARY),故通过线是各侧 ≥90% 而非 100%,如实反映近似性。

改了 SKILL.md description 的触发词/闸门后,应回来同步本文件的 TRIGGER_WORDS 与用例。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# --- 与 SKILL.md description 同步的显式触发词 ---
TRIGGER_WORDS = [
    "moa模式", "moa", "多人评审", "多人委员会", "委员会", "多模型",
    "第二意见", "交叉验证", "对上面的分析做出建议", "对上面的总结做出建议", "council",
]
# 判断类任务信号(自调触发:难取舍/低置信/需外部视角)
JUDGMENT_SIGNALS = [
    "评审", "决策", "选型", "头脑风暴", "拿不定主意", "哪个最优", "哪个更好",
    "做出建议", "没把握", "置信度", "方案", "review", "brainstorm", "decide", "评估",
]
# L0 闸门负信号:可机械验证(算术/事实检索/单一机械操作)
MECHANICAL_SIGNALS = [
    "计算", "等于几", "多少", "首都", "是什么意思", "翻译", "几点", "怎么读取",
    "改成", "列出", "前五位", "命令是什么", "语法错误", "第几行", "加", "乘以", "状态码",
]


def should_trigger(text: str) -> bool:
    """description 明文规则的关键词近似。返回是否应启动 MoA。"""
    low = text.lower()
    if any(w in text or w in low for w in TRIGGER_WORDS):
        return True
    has_judgment = any(w in text or w in low for w in JUDGMENT_SIGNALS)
    has_mechanical = any(w in text or w in low for w in MECHANICAL_SIGNALS)
    if has_mechanical and not has_judgment:  # L0 闸门:纯机械/事实问题不启动
        return False
    return has_judgment


# (utterance, expected_should_trigger)
POSITIVE = [
    ("用 moa 模式评审这份架构设计", True),
    ("组织一个委员会评审我们的登录流程", True),
    ("对上面的分析做出建议", True),
    ("对上面的总结做出建议", True),
    ("PostgreSQL 还是 MongoDB,帮我做技术选型决策", True),
    ("给我一个第二意见,这个安全方案靠谱吗", True),
    ("多模型交叉验证一下这段合约代码有没有漏洞", True),
    ("头脑风暴一下我们产品的差异化方向", True),
    ("帮我评审这个 API 设计的可维护性", True),
    ("这三个缓存方案我拿不定主意,哪个最优", True),
    ("我对自己刚才的结论没把握,找几个模型交叉验证看看", True),
    ("council review this migration plan", True),
    # 边界:真判断但用词不在信号表内,关键词启发式会漏(Claude 语义判断能命中)
    ("这两种加密算法我该选哪个", True),
]
NEGATIVE = [
    ("2 加 2 等于几", False),
    ("计算 158 乘以 24", False),
    ("法国的首都是哪里", False),
    ("HTTP 200 状态码是什么意思", False),
    ("把这句话翻译成英文", False),
    ("现在几点了", False),
    ("Python 里怎么读取一个文件", False),
    ("这个 JSON 里 user_id 的值是多少", False),
    ("帮我把变量名改成驼峰命名", False),
    ("列出这个目录下的所有文件", False),
    ("圆周率的前五位是多少", False),
    ("git commit 的命令是什么", False),
    # 边界:含"方案"但实为事实检索,关键词启发式会误触发(Claude 语义判断能拦下)
    ("这个方案的作者是谁", False),
]

# 已知边界失配:关键词启发式与真实语义判断在此二例分歧,计入通过线余量。
KNOWN_BOUNDARY = {"这两种加密算法我该选哪个", "这个方案的作者是谁"}


def _accuracy(cases):
    hits = sum(1 for text, exp in cases if should_trigger(text) == exp)
    return hits, len(cases)


def test_positive_trigger_rate_at_least_90pct():
    hits, total = _accuracy(POSITIVE)
    misses = [t for t, e in POSITIVE if should_trigger(t) != e]
    assert hits / total >= 0.9, f"正例通过率 {hits}/{total} < 90%; 漏判: {misses}"


def test_negative_trigger_rate_at_least_90pct():
    hits, total = _accuracy(NEGATIVE)
    misses = [t for t, e in NEGATIVE if should_trigger(t) != e]
    assert hits / total >= 0.9, f"负例通过率 {hits}/{total} < 90%; 误触发: {misses}"


def test_case_set_meets_minimum_size():
    # dev-plan 验收:各侧 ≥10 例
    assert len(POSITIVE) >= 10 and len(NEGATIVE) >= 10


def test_known_boundary_cases_are_the_only_misses():
    # 失配只允许出现在已登记的边界例上;非边界例失配 = 启发式退化,须回来修
    misses = {t for t, e in POSITIVE + NEGATIVE if should_trigger(t) != e}
    assert misses <= KNOWN_BOUNDARY, f"出现未登记失配: {misses - KNOWN_BOUNDARY}"
