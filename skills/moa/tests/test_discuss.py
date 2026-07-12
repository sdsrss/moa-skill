"""开会讨论模式(§6 阶段5)离线测试:transcript 格式化、prompt 构造、从众/假讨论/漂移统计、注入。

真实顺序回合 + 盲投的端到端在 E2E(moa-reports/e2e-discuss)。此处只测纯逻辑,无网络。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import moa  # noqa: E402


def _turn(seat, role, stance, new_arg="", changed=False, by_new=False, responses=None, rnd=1,
          usage=None, still="立场"):
    return {"round": rnd, "seat": seat, "role": role, "channel_used": "api",
            "model_used": "m", "usage": usage, "latency_s": 1.0, "error": None, "err_class": None,
            "turn": {"still_holding": still, "responses": responses or [],
                     "new_argument": new_arg, "position_changed": changed,
                     "changed_by_new_argument": by_new, "current_stance": stance, "confidence": 0.7}}


# ---------- 发言署名不暴露模型 ----------

def test_speaker_label_hides_model():
    lbl = moa._speaker_label({"seat": "A", "role": "security_auditor", "model_used": "gpt-4o"})
    assert lbl == "委员A(security_auditor)" and "gpt" not in lbl


# ---------- transcript 格式化 ----------

def test_format_transcript_empty_marks_first_speaker():
    assert "第一位发言" in moa.format_transcript([])


def test_format_transcript_groups_by_round_and_hides_model():
    turns = [_turn("A", "sec", "必须修", new_arg="彩虹表", rnd=1, usage={"total_tokens": 5}),
             _turn("B", "ship", "下迭代", responses=[{"stance": "rebut", "to": "委员A", "reason": "内网低危"}], rnd=1)]
    s = moa.format_transcript(turns)
    assert "第 1 轮" in s and "委员A(sec)" in s and "委员B(ship)" in s
    assert "彩虹表" in s and "rebut" in s
    assert "gpt" not in s and "total_tokens" not in s   # 不泄模型/内部字段


def test_format_transcript_skips_failed_turns():
    turns = [_turn("A", "sec", "x"), {"round": 1, "seat": "B", "role": "y", "turn": None}]
    s = moa.format_transcript(turns)
    assert "委员A" in s and "委员B" not in s


# ---------- prompt 构造(讨论 vs 盲投) ----------

def test_discuss_prompt_contains_preamble_role_schema_and_transcript():
    m = {"seat": "A", "role": "tester"}
    system, user = moa.discuss_prompt(m, "review", "简报内容XY", "此前发言ZZ", 3, {"tester": "你是测试角色"})
    assert "开会讨论" in system and "你是测试角色" in system and "still_holding" in system
    assert "简报内容XY" in user and "此前发言ZZ" in user and "第 3 轮" in user


def test_blind_prompt_has_no_transcript():
    m = {"seat": "A", "role": "tester"}
    system, user = moa.discuss_prompt(m, "review", "简报XY", "机密发言记录", 3, {"tester": "T"}, blind=True)
    assert "不参考任何讨论记录" in system and "final_stance" in system
    assert "简报XY" in user and "机密发言记录" not in user   # 盲投不喂 transcript


# ---------- 注入(CH1 子代理回填) ----------

def test_inject_result_marks_subagent_and_unbilled():
    m = {"name": "sec-a", "seat": "A", "role": "security_auditor", "model": "claude-haiku-4-5"}
    res = moa._inject_result(m, "review", {"current_stance": "必须修"})
    assert res["channel_used"] == "subagent (arbiter-dispatched)"
    assert res["usage"] is None and res["model_used"] == "claude-haiku-4-5"
    assert res["parsed"] == {"current_stance": "必须修"} and res["err_class"] is None


# ---------- transcript 落盘往返 ----------

def test_transcript_append_and_load_roundtrip(tmp_path):
    moa.append_transcript(tmp_path, {"round": 1, "seat": "A", "turn": {"current_stance": "x"}})
    moa.append_transcript(tmp_path, {"round": 1, "seat": "B", "turn": {"current_stance": "y"}})
    loaded = moa.load_transcript(tmp_path)
    assert len(loaded) == 2 and loaded[0]["seat"] == "A" and loaded[1]["seat"] == "B"


def test_load_transcript_missing_is_empty(tmp_path):
    assert moa.load_transcript(tmp_path) == []


# ---------- 统计: 从众 / 假讨论 / 漂移 / 保留分歧 ----------

def test_conformity_alert_flags_change_without_new_argument():
    transcript = [
        _turn("A", "sec", "必须修", new_arg="彩虹表", rnd=1, usage={"total_tokens": 10}),
        _turn("B", "ship", "下迭代", rnd=1, usage={"total_tokens": 8}),
        # round2: A 无新论据却翻立场 → 从众
        _turn("A", "sec", "下迭代", new_arg="", changed=True, by_new=False, rnd=2, usage={"total_tokens": 9}),
        _turn("B", "ship", "下迭代", new_arg="", rnd=2, usage=None),
        {"round": 1, "seat": "C", "role": "z", "turn": None, "err_class": "transient"},
    ]
    blindvotes = [{"seat": "A", "vote": {"final_stance": "必须修", "confidence": 0.9}, "usage": None}]
    st = moa.compute_discuss_stats(transcript, blindvotes)
    assert st["rounds"] == 2 and st["turns_ok"] == 4 and st["turns_failed"] == 1
    assert st["participants"] == ["A", "B"]
    assert st["conformity_alert"] is True and len(st["conformity_alerts"]) == 1
    assert st["conformity_alerts"][0]["seat"] == "A" and st["conformity_alerts"][0]["round"] == 2
    # round2 全员 new_argument 空 → 假讨论;且是末轮 → 建议早停
    assert st["pseudo_discussion_rounds"] == [2] and st["early_stop_suggested"] is True
    # 盲投漂移对照: A 讨论终态=下迭代 但盲投=必须修(讨论诱发漂移的证据)
    a_pair = next(p for p in st["blind_vote_drift_pairs"] if p["seat"] == "A")
    assert a_pair["discussion_final"] == "下迭代" and a_pair["blind_final"] == "必须修"
    b_pair = next(p for p in st["blind_vote_drift_pairs"] if p["seat"] == "B")
    assert b_pair["blind_final"] is None            # B 无盲投
    # token: 只累计计费回合(usage 非空),CH1/None 不计。讨论按回合计费,故键为 billed_calls(C6)
    assert st["token_usage"]["total_tokens"] == 27 and st["token_usage"]["billed_calls"] == 3


def test_argument_driven_change_is_not_conformity():
    transcript = [
        _turn("A", "sec", "必须修", new_arg="彩虹表", rnd=1),
        _turn("B", "ship", "必须修", new_arg="", changed=True, by_new=True, rnd=1),  # 被新论据说服
    ]
    st = moa.compute_discuss_stats(transcript, [])
    assert st["conformity_alert"] is False          # 有新论据的改变=收敛,不算从众


def test_dissent_preserved_reports_final_positions():
    transcript = [
        _turn("A", "sec", "必须修", still="无盐是 blocker", rnd=1),
        _turn("B", "ship", "下迭代", still="工期优先",
              responses=[{"stance": "rebut", "to": "委员A", "reason": "内网低危可暂缓"}], rnd=1),
    ]
    st = moa.compute_discuss_stats(transcript, [])
    holds = {d["seat"]: d for d in st["dissent_preserved"]}
    assert holds["A"]["still_holding"] == "无盐是 blocker"
    assert holds["B"]["open_rebuttals"] == ["内网低危可暂缓"]
