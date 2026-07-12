"""moa.py 离线测试套件(无网络)。

运行: cd skills/moa && python -m pytest tests/ -q
覆盖: 配置/角色解析、代理判定、错误分类、JSON 修复、统计块、通道调度、
      fallback 展开、Quorum 宽限窗、endpoint 构造。真实 API/CLI 调用不在此(见 E2E)。
"""
import json
import os
import sys
import time
import types
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import moa  # noqa: E402


# ---------- parse_json / JSON 修复提取 ----------

@pytest.mark.parametrize("text,expect", [
    ('{"a":1}', {"a": 1}),
    ('```json\n{"a":1}\n```', {"a": 1}),
    ('这是我的判断:\n{"a":1,"b":[2,3]}\n以上。', {"a": 1, "b": [2, 3]}),
    ('no json here', None),
    ('{bad json', None),
])
def test_parse_json(text, expect):
    assert moa.parse_json(text) == expect


# ---------- 代理判定 no_proxy 边界 ----------

def test_bypass_proxy_localhost():
    assert moa._bypass_proxy("localhost")
    assert moa._bypass_proxy("127.0.0.1")
    assert moa._bypass_proxy("::1")


def test_bypass_proxy_no_proxy_suffix(monkeypatch):
    monkeypatch.setenv("no_proxy", "example.com,.internal")
    assert moa._bypass_proxy("example.com")
    assert moa._bypass_proxy("api.internal")      # 后缀匹配
    assert moa._bypass_proxy("internal")          # .internal 去点后精确匹配
    assert not moa._bypass_proxy("example.org")
    assert not moa._bypass_proxy("notexample.com")  # 非子域,不匹配


# ---------- 错误分类 (瞬态 vs 永久) ----------

def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "msg", {}, None)


def test_classify_429_transient():
    e = moa.classify_http_error(_http_error(429))
    assert isinstance(e, moa.TransientError) and e.err_class == "rate_limit"


def test_classify_500_transient():
    assert isinstance(moa.classify_http_error(_http_error(503)), moa.TransientError)


def test_classify_401_permanent():
    e = moa.classify_http_error(_http_error(401))
    assert isinstance(e, moa.PermanentError) and e.err_class == "auth"


def test_classify_404_permanent():
    assert isinstance(moa.classify_http_error(_http_error(404)), moa.PermanentError)


# ---------- endpoint 与 headers 构造 ----------

def test_endpoint_openrouter_defaults(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    url, headers = moa.endpoint_and_headers({"protocol": "openrouter", "model": "x/y"})
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert "HTTP-Referer" in headers and "X-Title" in headers


def test_endpoint_openai_defaults(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    url, headers = moa.endpoint_and_headers({"protocol": "openai", "model": "gpt"})
    assert url == "https://api.openai.com/v1/chat/completions"
    assert "HTTP-Referer" not in headers  # openai 不带归因头


def test_endpoint_missing_key_permanent(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(moa.PermanentError) as ei:
        moa.endpoint_and_headers({"protocol": "openrouter", "model": "x"})
    assert ei.value.err_class == "auth"


def test_endpoint_custom_base_and_keyenv(monkeypatch):
    monkeypatch.setenv("MYKEY", "k")
    url, _ = moa.endpoint_and_headers(
        {"protocol": "openai", "model": "m", "base_url": "http://local:8000/v1", "api_key_env": "MYKEY"})
    assert url == "http://local:8000/v1/chat/completions"


# ---------- 角色解析: references 命中 + custom_roles 覆盖 ----------

def test_role_resolves_from_references():
    for key in ("security_auditor", "feasibility_skeptic", "user_advocate", "maintainability_reviewer"):
        p = moa.load_role_prompt("review", key, {})
        assert not p.startswith("你的角色是"), f"{key} 未命中 references"
        assert len(p) > 20


def test_role_custom_override_wins():
    assert moa.load_role_prompt("review", "security_auditor", {"security_auditor": "CUSTOM"}) == "CUSTOM"


def test_role_unknown_falls_back():
    assert moa.load_role_prompt("review", "nonexistent_role", {}).startswith("你的角色是")


# ---------- 通道调度 / fallback 展开 / dispatchable ----------

def test_resolve_channel_api():
    tries = moa.resolve_channel({"name": "x", "channel": "api", "model": "m"})
    assert [t[0] for t in tries] == ["api"]


def test_resolve_channel_cli_with_api_fallback():
    m = {"name": "x", "channel": "cli", "model": "gpt",
         "fallback": [{"channel": "api", "protocol": "openrouter", "model": "openai/gpt"}]}
    kinds = [t[0] for t in moa.resolve_channel(m)]
    assert kinds == ["cli", "api"]
    # fallback 合并了 member 基础字段
    _, cfg, note = moa.resolve_channel(m)[1]
    assert cfg["model"] == "openai/gpt" and "fallback" in note


def test_resolve_channel_subagent_skipped_without_fallback():
    m = {"name": "x", "channel": "subagent", "model": "claude"}
    assert moa.resolve_channel(m) == []


def test_resolve_channel_subagent_with_api_fallback():
    m = {"name": "x", "channel": "subagent", "model": "claude",
         "fallback": [{"channel": "api", "model": "anthropic/claude"}]}
    assert [t[0] for t in moa.resolve_channel(m)] == ["api"]


def test_has_dispatchable_channel():
    assert moa._has_dispatchable_channel({"channel": "api"})
    assert moa._has_dispatchable_channel({"channel": "cli"})
    assert not moa._has_dispatchable_channel({"channel": "subagent"})
    assert moa._has_dispatchable_channel(
        {"channel": "subagent", "fallback": [{"channel": "api"}]})


def test_effective_billing_matches_actual_run():
    """dry-run 计费判定须与 moa.py 真正会跑的通道一致(回归 dry-run 少报 bug):
    旧逻辑只看主通道,把'subagent + api fallback'误记为免费订阅,而 generate 实际走计费 API。"""
    # 纯 subagent(无 api/cli fallback)= 仲裁人免费派发
    assert moa._effective_billing({"channel": "subagent", "model": "claude"}) == "sub"
    # subagent + api fallback = 脚本实跑计费 API(旧逻辑误记为免费,回归点)
    assert moa._effective_billing(
        {"channel": "subagent", "model": "claude",
         "fallback": [{"channel": "api", "model": "anthropic/claude"}]}) == "billed"
    # subagent + cli fallback = 订阅(codex 也免费)
    assert moa._effective_billing(
        {"channel": "subagent", "model": "c",
         "fallback": [{"channel": "cli", "model": "gpt"}]}) == "sub"
    # cli(codex)= 订阅免费;api = 计费
    assert moa._effective_billing({"channel": "cli", "model": "gpt"}) == "sub"
    assert moa._effective_billing({"channel": "api", "model": "m"}) == "billed"


def test_dispatch_cli_without_model_no_keyerror(monkeypatch):
    """codex(cli)席可省 model(用 codex 默认);结果构造须给 model_used=None,不得 KeyError。
    回归:_dispatch_channels 曾用 ccfg['model'] 硬取键,codex 成功后崩在结果构造上。"""
    monkeypatch.setattr(moa, "call_cli_codex",
                        lambda ccfg, system, user, timeout: ('{"verdict":"pass"}', {"verdict": "pass"}))
    member = {"name": "skeptic-a", "seat": "A", "channel": "cli", "protocol": "codex"}  # 无 model
    opts = {"timeout_seconds": 60, "max_tokens_member": 100}
    res = moa._dispatch_channels(member, "feasibility_skeptic", "sys", "usr", opts)
    assert res["parsed"] == {"verdict": "pass"}
    assert res["model_used"] is None          # 省 model → None,非崩溃
    assert res["channel_used"] == "cli"
    assert res["err_class"] is None


# ---------- 统计块: 按模式分支 + 分母只计成功 + degraded ----------

def _res(name, seat, parsed, err_class=None):
    return {"name": name, "seat": seat, "model_used": "m", "channel_used": "api",
            "parsed": parsed, "err_class": err_class, "error": None if parsed else "x"}


def test_stats_review_denominator_ok_only():
    results = [
        _res("a", "A", {"verdict": "fail", "confidence": 0.8,
                        "issues": [{"severity": "blocker"}, {"severity": "low"}]}),
        _res("b", "C", {"verdict": "fail", "confidence": 1.0,
                        "issues": [{"severity": "blocker"}]}),
        _res("c", "D", None, err_class="server"),
    ]
    s = moa.compute_stats("review", results)
    assert s["members_ok"] == 2 and s["members_failed"] == 1
    assert s["degraded"] is True
    assert s["issue_count_by_severity"]["blocker"] == 2
    assert s["mean_confidence"] == 0.9          # (0.8+1.0)/2,失败席不进分母
    assert s["verdict_tally"] == {"fail": 2}
    assert s["failures"][0]["err_class"] == "server"


def test_stats_not_degraded_when_all_ok():
    results = [_res("a", "A", {"verdict": "pass", "confidence": 0.5, "issues": []})]
    s = moa.compute_stats("review", results)
    assert s["degraded"] is False


def test_stats_decide_branch():
    results = [
        _res("a", "A", {"claimed_option": "PostgreSQL", "confidence": 0.7,
                        "opponent_fatal_flaws": [{"option": "Mongo", "severity": "fatal"}],
                        "spike_suggestion": "benchmark 10min"}),
        _res("b", "C", {"claimed_option": "PostgreSQL", "confidence": 0.9,
                        "opponent_fatal_flaws": [], "spike_suggestion": ""}),
    ]
    s = moa.compute_stats("decide", results)
    assert s["option_claims"] == {"PostgreSQL": 2}
    assert s["flaw_count_by_severity"]["fatal"] == 1
    assert s["spike_suggestions"] == 1


def test_stats_brainstorm_branch():
    results = [
        _res("a", "A", {"ideas": [{"novelty": 5}, {"novelty": 2}]}),
        _res("b", "D", {"ideas": [{"novelty": 4}]}),
    ]
    s = moa.compute_stats("brainstorm", results)
    assert s["total_ideas_before_dedup"] == 3
    assert s["high_novelty_ideas"] == 2  # novelty>=4 的两条


def test_merge_usage_sums_and_tolerates_missing():
    m = moa._merge_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                         None,
                         {"prompt_tokens": 2, "total_tokens": 2})  # 缺 completion_tokens
    assert m == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


def test_stats_token_usage_billed_only():
    # 计费席(有 usage)与订阅席(usage=None)混合:只累加计费席,billed_members 计数
    a = _res("a", "A", {"verdict": "pass", "confidence": 0.5, "issues": []})
    a["usage"] = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    b = _res("b", "B", {"verdict": "pass", "confidence": 0.5, "issues": []})
    b["usage"] = {"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110}
    c = _res("c", "C", {"verdict": "pass", "confidence": 0.5, "issues": []})
    c["usage"] = None  # 订阅席(codex),不计费
    s = moa.compute_stats("review", [a, b, c])
    tu = s["token_usage"]
    assert tu["billed_members"] == 2          # 只有 a、b 计费
    assert tu["total_tokens"] == 250          # 140 + 110,订阅席不计入
    assert tu["prompt_tokens"] == 180


# ---------- custom 模式: --members/--models(SKILL.md 承诺的入口)----------

def test_build_custom_members_from_models_list():
    ms = moa.build_custom_members("openai/gpt-5,anthropic/claude-opus-4.8,google/gemini-3.1-pro")
    assert [m["seat"] for m in ms] == ["A", "B", "C"]
    assert [m["model"] for m in ms] == ["openai/gpt-5", "anthropic/claude-opus-4.8", "google/gemini-3.1-pro"]
    assert all(m["channel"] == "api" and m["protocol"] == "openrouter" for m in ms)
    assert [m["name"] for m in ms] == ["custom-a", "custom-b", "custom-c"]


def test_build_custom_members_self_moa_replicate():
    # 单模型 + --members N = 主动 Self-MoA:复制成 N 席(座位分化角色)
    ms = moa.build_custom_members("openai/gpt-5", members_n=3)
    assert len(ms) == 3
    assert all(m["model"] == "openai/gpt-5" for m in ms)
    assert [m["seat"] for m in ms] == ["A", "B", "C"]


def test_build_custom_members_explicit_dup_is_self_moa():
    ms = moa.build_custom_members("x,x")           # 显式重复 = Self-MoA
    assert [m["model"] for m in ms] == ["x", "x"]


def test_build_custom_members_matching_count_ok():
    assert len(moa.build_custom_members("a,b,c", members_n=3)) == 3


@pytest.mark.parametrize("csv,n", [("a,b", 3), ("a,b,c", 2)])
def test_build_custom_members_count_mismatch_errors(csv, n):
    with pytest.raises(SystemExit):
        moa.build_custom_members(csv, members_n=n)


def test_build_custom_members_over_cap_errors():
    with pytest.raises(SystemExit):
        moa.build_custom_members("a,b,c,d,e")      # 上限 4 席


@pytest.mark.parametrize("csv", ["", "  ", " , , "])
def test_build_custom_members_empty_errors(csv):
    with pytest.raises(SystemExit):
        moa.build_custom_members(csv)


def test_apply_custom_committee_overrides_members():
    cfg = {"members": [{"name": "orig", "seat": "A"}], "options": {"max_tokens_member": 3000},
           "custom_roles": {"r": "x"}}
    args = types.SimpleNamespace(models="a,b", members=None)
    out = moa.apply_custom_committee(cfg, args)
    assert [m["model"] for m in out["members"]] == ["a", "b"]   # members 被覆盖
    assert out["options"] == cfg["options"] and out["custom_roles"] == cfg["custom_roles"]  # 其余保留
    assert cfg["members"][0]["name"] == "orig"                  # 原 cfg 未被就地改写


def test_apply_custom_committee_noop_without_models():
    cfg = {"members": [{"name": "orig"}], "options": {}}
    args = types.SimpleNamespace(models=None, members=None)
    assert moa.apply_custom_committee(cfg, args) is cfg          # 无 --models 原样返回


# ---------- Quorum 宽限窗 ----------

def test_dispatch_quorum_grace_skips_straggler():
    """3 席: 2 快 1 慢。quorum=2,grace 极短 → 慢席被标 skipped_grace。"""
    members = [{"name": "fast1", "seat": "A"}, {"name": "fast2", "seat": "B"},
               {"name": "slow", "seat": "C"}]

    def fn(m):
        if m["name"] == "slow":
            time.sleep(2.0)
        return {"name": m["name"], "seat": m["seat"], "parsed": {"ok": 1},
                "role": "r", "channel_used": "api", "latency_s": 0.0,
                "model_used": "m", "err_class": None, "error": None}

    written = []
    res = moa.dispatch_with_quorum(members, fn, quorum_target=2, grace_s=0.1,
                                   on_done=lambda r: written.append(r["name"]))
    by = {r["name"]: r for r in res}
    assert by["fast1"]["parsed"] and by["fast2"]["parsed"]
    assert by["slow"]["err_class"] == "skipped_grace"
    assert by["slow"]["parsed"] is None
    assert set(written) == {"fast1", "fast2", "slow"}  # 全部落盘


def test_dispatch_no_grace_when_all_fast():
    members = [{"name": "a", "seat": "A"}, {"name": "b", "seat": "B"}]
    fn = lambda m: {"name": m["name"], "seat": m["seat"], "parsed": {"ok": 1},
                    "role": "r", "channel_used": "api", "latency_s": 0.0,
                    "model_used": "m", "err_class": None, "error": None}
    res = moa.dispatch_with_quorum(members, fn, quorum_target=1, grace_s=5.0)
    assert all(r["parsed"] for r in res) and len(res) == 2


# ---------- min_successful 动态阈值(逻辑) ----------

@pytest.mark.parametrize("configured,seats,expect", [
    (2, 4, 2), (2, 1, 1), (2, 2, 2), (3, 2, 2),
])
def test_min_successful_dynamic(configured, seats, expect):
    assert min(configured, max(1, seats)) == expect


# ---------- M3: 匿名化 ----------

def test_anonymize_excludes_self_and_failed():
    res = [
        {"name": "a", "parsed": {"v": 1}},
        {"name": "b", "parsed": {"v": 2}},
        {"name": "c", "parsed": None},          # 失败席不进匿名池
    ]
    out = moa.anonymize_others(res, "a")
    assert out == [{"评审员": "甲", "意见": {"v": 2}}]  # 排除自己 a + 失败 c


def test_anonymize_relabels_sequentially():
    res = [{"name": n, "parsed": {"i": i}} for i, n in enumerate("abcd")]
    out = moa.anonymize_others(res, "a")
    assert [o["评审员"] for o in out] == ["甲", "乙", "丙"]


# ---------- M3: 决策/头脑风暴 seat 角色 + 精炼 schema ----------

def test_brainstorm_seat_roles_resolve():
    for seat in "ABCD":
        key = moa.DEFAULT_SEAT_ROLE[("brainstorm", seat)]
        assert not moa.load_role_prompt("brainstorm", key, {}).startswith("你的角色是")


def test_refine_schemas_exist():
    assert set(moa.REFINE_SCHEMAS) == {"review", "decide"}
    assert "brainstorm" not in moa.REFINE_SCHEMAS  # 头脑风暴无精炼轮


# ---------- M3: 精炼统计 — 三态/disputed/谄媚/早停 ----------

def _rf(name, verdict, verdicts_on_others):
    return {"name": name, "seat": "A",
            "parsed": {"verdict": verdict, "verdicts_on_others": verdicts_on_others}}


def test_refine_stats_three_state_and_disputed():
    prior = [_rf("a", "fail", []), _rf("b", "fail", []), _rf("c", "pass", [])]
    refine = [
        _rf("a", "fail", [{"ref_title": "X", "stance": "challenge", "reason": "误报"},
                          {"ref_title": "Y", "stance": "validate"}]),
        _rf("b", "fail", [{"ref_title": "Z", "stance": "abstain"}]),
        _rf("c", "pass", [{"ref_title": "X", "stance": "validate"}]),
    ]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["stance_tally"] == {"validate": 2, "challenge": 1, "abstain": 1}
    assert s["disputed_titles"] == ["X"]          # 一票 challenge 即锁 disputed
    assert s["early_stop_suggested"] is False      # verdict 不一致 + 有 disputed


def test_refine_stats_sycophancy_alert():
    # 上一轮多数 = fail(2 fail vs 1 pass)。本轮 b、c 无理由(无 challenge)从 pass 翻向 fail → 谄媚
    prior = [_rf("a", "fail", []), _rf("b", "fail", []),
             _rf("c", "pass", []), _rf("d", "pass", [])]
    refine = [
        _rf("a", "fail", []),
        _rf("b", "fail", []),
        _rf("c", "fail", []),   # pass->fail 翻向上一轮多数派 fail,且未提 challenge
        _rf("d", "fail", []),   # 同上
    ]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["sycophancy_detail"]["prior_majority_verdict"] == "fail"
    assert s["sycophancy_alert"] is True
    assert s["sycophancy_detail"]["movers"] == 2
    assert s["sycophancy_detail"]["flips_toward_majority"] == 2


def test_refine_stats_challenge_is_not_sycophancy():
    # b 翻向多数,但提出了 challenge(有新证据代理)→ 不算谄媚
    prior = [_rf("a", "fail", []), _rf("b", "pass", [])]
    refine = [
        _rf("a", "fail", []),
        _rf("b", "fail", [{"ref_title": "X", "stance": "challenge", "reason": "r"}]),
    ]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["sycophancy_alert"] is False


def test_refine_stats_early_stop_when_consensus():
    prior = [_rf("a", "fail", []), _rf("b", "pass", [])]
    refine = [_rf("a", "fail", []), _rf("b", "fail", [])]  # 全一致 + 无 challenge
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["early_stop_suggested"] is True


def test_refine_stats_decide_cross_exam():
    prior = [{"name": "a", "seat": "A", "parsed": {"claimed_option": "PG"}},
             {"name": "b", "seat": "C", "parsed": {"claimed_option": "Mongo"}}]
    refine = [
        {"name": "a", "seat": "A", "parsed": {
            "revised_claimed_option": "PG",
            "cross_exam": [{"target_option": "Mongo", "attack_severity": "fatal"}]}},
        {"name": "b", "seat": "C", "parsed": {
            "revised_claimed_option": "PG",   # 从 Mongo 改投 PG = option shift
            "cross_exam": [{"target_option": "PG", "attack_severity": "minor"}]}},
    ]
    s = moa.compute_refine_stats("decide", prior, refine)
    assert s["cross_exam_by_severity"]["fatal"] == 1
    assert s["cross_exam_by_severity"]["minor"] == 1
    assert s["option_shifts"] == 1
    assert s["early_stop_suggested"] is True    # 两席最终都投 PG


# ---------- 产物读写 round-trip + 精炼产物排除 ----------

def test_member_write_load_roundtrip(tmp_path):
    r = _res("a", "A", {"verdict": "pass", "confidence": 0.5, "issues": []})
    moa.write_member(tmp_path, r)
    moa.write_member(tmp_path, {**r, "name": "b"}, round_no=1)  # 精炼产物
    gen = moa.load_members(tmp_path, round_no=0)
    assert [m["name"] for m in gen] == ["a"]  # round_no=0 排除 .r1
    r1 = moa.load_members(tmp_path, round_no=1)
    assert [m["name"] for m in r1] == ["b"]
