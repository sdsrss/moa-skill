"""委员故障注入 E2E(requirements §12.5)。

三种故障 → 三种正确行为,各在真实 moa.py 函数上跑通(故障只在传输边界 http_post /
call_model 注入,重试/退避/分类/修复/中止全走真代码路径):
  1. 瞬态/超时  → call_model 指数退避重试(永久错误不重试)
  2. 非法 JSON  → call_with_json_repair 花一次修复调用自愈(合法则不浪费调用)
  3. 全体委员挂 → dispatch 返回 0 成功 → cmd_generate 的 min_ok 门中止

「全挂→中止」另有一条真实 API E2E(坏模型 ID → 全 404 → abort),见 moa-reports/e2e-fault/。
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import moa  # noqa: E402


def _proc(rc, out=b"", err=b""):
    """伪 subprocess.CompletedProcess,喂给 call_cli_codex 的分类分支测试。"""
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


# ---------- 行为 1: 瞬态/超时 → 重试;永久错误 → 立即抛 ----------

def test_timeout_triggers_retry_then_succeeds(monkeypatch):
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)          # 免退避等待
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    calls = {"n": 0}

    def flaky(url, headers, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("simulated timeout")                 # 首次超时
        return {"choices": [{"message": {"content": '{"verdict":"pass"}'}}],
                "usage": {"total_tokens": 5}}

    monkeypatch.setattr(moa, "http_post", flaky)
    content, usage = moa.call_model({"model": "m"}, "sys", "usr", 0.3, 100, 30)
    assert calls["n"] == 2                    # 重试了一次
    assert '"verdict"' in content


def test_permanent_error_not_retried(monkeypatch):
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    calls = {"n": 0}

    def always_auth_fail(url, headers, payload, timeout):
        calls["n"] += 1
        raise moa.PermanentError("401 unauthorized", err_class="auth")

    monkeypatch.setattr(moa, "http_post", always_auth_fail)
    with pytest.raises(moa.PermanentError):
        moa.call_model({"model": "m"}, "s", "u", 0.3, 100, 30)
    assert calls["n"] == 1                    # 永久错误立即抛,绝不重试


def test_retry_exhausted_raises_last_transient(monkeypatch):
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    calls = {"n": 0}

    def always_timeout(url, headers, payload, timeout):
        calls["n"] += 1
        raise TimeoutError("always down")

    monkeypatch.setattr(moa, "http_post", always_timeout)
    with pytest.raises((TimeoutError, moa.TransientError)):
        moa.call_model({"model": "m"}, "s", "u", 0.3, 100, 30)
    assert calls["n"] == 3                    # 首次 + 2 次重试 = 3 次尝试后放弃


# ---------- 行为 2: 非法 JSON → 单次修复自愈 ----------

def test_bad_json_triggers_single_repair(monkeypatch):
    seq = ["这是我的意见,结论是 pass,没有 JSON",       # 首次:无法解析
           '{"verdict":"pass","issues":[]}']            # 修复后:合法
    calls = {"n": 0}

    def fake_call_model(cfg, system, user, temp, max_tokens, timeout, retries=2, **_):
        i = calls["n"]
        calls["n"] += 1
        return seq[i], {"total_tokens": 3}

    monkeypatch.setattr(moa, "call_model", fake_call_model)
    raw, parsed, usage = moa.call_with_json_repair({"model": "m"}, "s", "u", 0.3, 100, 30)
    assert calls["n"] == 2                    # 发生了 1 次修复调用
    assert parsed == {"verdict": "pass", "issues": []}
    assert usage["total_tokens"] == 6         # 两次调用 usage 累加(_merge_usage)


def test_valid_json_needs_no_repair(monkeypatch):
    calls = {"n": 0}

    def fake_call_model(cfg, system, user, temp, max_tokens, timeout, retries=2, **_):
        calls["n"] += 1
        return '{"verdict":"pass"}', {"total_tokens": 3}

    monkeypatch.setattr(moa, "call_model", fake_call_model)
    raw, parsed, usage = moa.call_with_json_repair({"model": "m"}, "s", "u", 0.3, 100, 30)
    assert calls["n"] == 1                    # 一次成功就不浪费修复调用
    assert parsed == {"verdict": "pass"}


def test_repair_also_fails_returns_none(monkeypatch):
    def fake_call_model(cfg, system, user, temp, max_tokens, timeout, retries=2, **_):
        return "还是没有 JSON", {"total_tokens": 1}   # 修复也失败

    monkeypatch.setattr(moa, "call_model", fake_call_model)
    raw, parsed, usage = moa.call_with_json_repair({"model": "m"}, "s", "u", 0.3, 100, 30)
    assert parsed is None                     # 修复无果 → parsed=None,交由上层降级为失败席


# ---------- 行为 3: 全体委员挂 → 0 成功(cmd_generate min_ok 门会中止) ----------

def test_all_members_fail_yields_zero_ok():
    members = [{"name": "a", "seat": "A", "channel": "api", "model": "bad"},
               {"name": "b", "seat": "B", "channel": "api", "model": "bad"}]

    def always_fail(m):
        return moa._fail(m, "feasibility_skeptic", "boom", "transient")

    results = moa.dispatch_with_quorum(members, always_fail, quorum_target=2, grace_s=0)
    ok = [r for r in results if r["parsed"]]
    assert len(ok) == 0                       # 全挂 → 0 成功;cmd_generate 据此 sys.exit 中止
    assert all(r["err_class"] == "transient" for r in results)


# ---------- CH2 codex CLI 通道:错误分类分支(补测,cli 路径此前无单测) ----------

def test_cli_codex_missing_binary_is_permanent(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: None)          # codex 不在 PATH
    with pytest.raises(moa.PermanentError) as ei:
        moa.call_cli_codex({"codex_bin": "nope"}, "s", "u", 5)
    assert ei.value.err_class == "startup"


def test_cli_codex_timeout_is_transient(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/codex")

    def boom(*a, **k):
        raise moa.subprocess.TimeoutExpired(cmd="codex", timeout=1)

    monkeypatch.setattr(moa.subprocess, "run", boom)
    with pytest.raises(moa.TransientError) as ei:
        moa.call_cli_codex({"codex_bin": "codex"}, "s", "u", 1)
    assert ei.value.err_class == "timeout"


def test_cli_codex_auth_error_is_permanent(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/codex")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(1, err=b"401 unauthorized: please login"))
    with pytest.raises(moa.PermanentError) as ei:
        moa.call_cli_codex({}, "s", "u", 5)
    assert ei.value.err_class == "auth"       # stderr 含 login/auth/401 → 永久,不重试


def test_cli_codex_generic_nonzero_is_transient(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/codex")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(2, err=b"transient upstream hiccup"))
    with pytest.raises(moa.TransientError):    # 非 auth 的非零退出 → 瞬态,可降级/重试
        moa.call_cli_codex({}, "s", "u", 5)


def test_cli_codex_empty_output_is_transient(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/codex")
    monkeypatch.setattr(moa.subprocess, "run", lambda *a, **k: _proc(0, out=b"   "))
    with pytest.raises(moa.TransientError) as ei:
        moa.call_cli_codex({}, "s", "u", 5)
    assert ei.value.err_class == "empty"      # 配额耗尽会产空壳 → 瞬态


def test_cli_codex_success_parses_stdout(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/codex")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(0, out=b'{"verdict":"pass"}'))
    out, parsed = moa.call_cli_codex({}, "s", "u", 5)   # last.txt 不存在 → 回退读 stdout
    assert parsed == {"verdict": "pass"}


# ---------- 通道调度: fallback 链遍历(核心韧性承诺,此前无端到端单测) ----------

def test_dispatch_channels_falls_through_to_fallback(monkeypatch):
    """主通道挂 → 沿 fallback 链降级到下一条 api 席并成功;model_used/channel_used 反映实走的那条。"""
    member = {"name": "a", "seat": "A", "channel": "api", "protocol": "openrouter",
              "model": "primary-down",
              "fallback": [{"channel": "api", "protocol": "openrouter", "model": "backup-up"}]}

    def fake_repair(ccfg, system, user, temp, max_tokens, timeout, schema, **_):
        if ccfg["model"] == "primary-down":
            raise moa.TransientError("primary 503", err_class="server")
        return "{}", {"verdict": "pass"}, {"total_tokens": 3}

    monkeypatch.setattr(moa, "call_with_json_repair", fake_repair)
    res = moa._dispatch_channels(member, "r", "sys", "usr",
                                 {"timeout_seconds": 30, "max_tokens_member": 100})
    assert res["parsed"] == {"verdict": "pass"}
    assert res["model_used"] == "backup-up"                 # 实走 fallback 那条
    assert "fallback from channel=api" in res["channel_used"]


def test_dispatch_channels_all_fail_returns_last_failure(monkeypatch):
    """主通道 + 全部 fallback 都挂 → 返回失败席(parsed=None),带最后一次错误分类。"""
    member = {"name": "a", "seat": "A", "channel": "api", "model": "m1",
              "fallback": [{"channel": "api", "model": "m2"}]}

    def always_fail(ccfg, *a, **k):
        raise moa.PermanentError("404 client", err_class="client")

    monkeypatch.setattr(moa, "call_with_json_repair", always_fail)
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 30, "max_tokens_member": 100})
    assert res["parsed"] is None and res["err_class"] == "client"


def test_dispatch_channels_api_unparseable_falls_through(monkeypatch):
    """api 席「输出不可解析」(生成轮 + 修复轮都没救回来)必须继续降级到下一条 fallback。
    回归 ISSUE-006: 旧代码 api 分支对这一类失败直接 return parsed=None 占掉整席,后续 fallback
    全部作废——而 cli 分支同样情形是 raise→降级。两条通道对同一类失败行为不对称,配了降级链
    的 api 席等于没配。此处走真实 call_with_json_repair(只 mock 最底层 call_model),
    以覆盖「生成失败 → 修复轮也失败」的完整路径。"""
    seen = []

    def fake_call_model(cfg, system, user, temp, max_tokens, timeout, retries=2, **_):
        seen.append(cfg["model"])
        if cfg["model"] == "primary-garbage":
            return "我先解释一下思路,然后这段不是 JSON", {"total_tokens": 100}
        return '{"verdict": "pass", "confidence": 0.8, "issues": []}', {"total_tokens": 50}

    monkeypatch.setattr(moa, "call_model", fake_call_model)
    member = {"name": "a", "seat": "A", "channel": "api", "model": "primary-garbage",
              "fallback": [{"channel": "api", "model": "backup-ok"}]}
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 30, "max_tokens_member": 100})
    assert "backup-ok" in seen                     # fallback 被真正尝试(回归点)
    assert res["parsed"] == {"verdict": "pass", "confidence": 0.8, "issues": []}
    assert res["model_used"] == "backup-ok"
    assert "fallback from channel=api" in res["channel_used"]


def test_dispatch_channels_api_unparseable_all_links_keeps_class_and_usage(monkeypatch):
    """全链都吐不可解析输出 → 失败席须带 err_class='parse' 与本链已花的 usage/raw。
    err_class=None(旧行为)让错误分类统计对这类失败失明;丢 usage 则已计费的生成+修复两次
    调用在产物里蒸发,成本统计恰在出问题时低估得最多;丢 raw 则人工无从抢救模型说了什么。"""

    def garbage(cfg, system, user, temp, max_tokens, timeout, retries=2, **_):
        return "还是不是 JSON", {"total_tokens": 20}

    monkeypatch.setattr(moa, "call_model", garbage)
    member = {"name": "a", "seat": "A", "channel": "api", "model": "m1",
              "fallback": [{"channel": "api", "model": "m2"}]}
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 30, "max_tokens_member": 100})
    assert res["parsed"] is None
    assert res["err_class"] == "parse"
    assert res["usage"]["total_tokens"] == 40      # 末条链的 生成 + 修复 两次调用之和
    assert res["raw"]                              # 原始文本保留,供人工抢救


# ---------- ISSUE-007: timeout_seconds = 每条 fallback 链的挂钟预算(非每次 HTTP 尝试)----------

def _fake_clock(monkeypatch):
    """可控单调钟: 让「消耗挂钟」在测试里变成确定性的算术,不用真的 sleep。"""
    clock = {"t": 0.0}
    monkeypatch.setattr(moa.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(moa.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    return clock


def test_link_budget_stops_retries_and_leaves_room_for_fallback(monkeypatch):
    """慢失败(每次尝试都把预算耗到超时)时,链 1 用满 timeout_seconds 即止,不再重试,
    链 2 拿到自己完整的一份预算。回归 ISSUE-007: 旧代码 timeout 是【每次尝试】的,
    单席最坏 = 链长 × (1+retries) × timeout(实测 3 链 × 3 次 × 240s = 2169s)。"""
    clock = _fake_clock(monkeypatch)
    attempts = []

    def burns_full_timeout(url, headers, payload, timeout):
        attempts.append(payload["model"])
        clock["t"] += timeout                      # 这次尝试一直挂到超时
        raise TimeoutError("simulated network timeout")

    monkeypatch.setattr(moa, "http_post", burns_full_timeout)
    member = {"name": "a", "seat": "A", "channel": "api", "model": "m1", "timeout_seconds": 100,
              "fallback": [{"channel": "api", "model": "m2"}]}
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 100, "max_tokens_member": 100})
    assert attempts.count("m1") == 1               # 链 1 不再吃掉整席时间
    assert attempts.count("m2") == 1               # 链 2 真的被试到(旧代码里它要等 300s 才轮到)
    assert clock["t"] == 200                       # 总挂钟 = 链长 × timeout, 而非 × (1+retries)
    assert res["parsed"] is None


def test_fast_failures_still_retry_within_link_budget(monkeypatch):
    """对照: 快速失败(如 429 秒回)几乎不消耗挂钟 → 重试次数与旧版一致。
    ISSUE-007 收紧的是【挂钟】,不是【重试策略】——这条防止修复把重试一起砍掉。"""
    clock = _fake_clock(monkeypatch)
    attempts = []

    def fails_instantly(url, headers, payload, timeout):
        attempts.append(payload["model"])          # 不推进 clock: 服务端秒回错误
        raise TimeoutError("instant")

    monkeypatch.setattr(moa, "http_post", fails_instantly)
    member = {"name": "a", "seat": "A", "channel": "api", "model": "m1", "timeout_seconds": 100}
    moa._dispatch_channels(member, "r", "s", "u",
                           {"timeout_seconds": 100, "max_tokens_member": 100})
    assert attempts.count("m1") == 3               # 1 次 + retries 2, 未被预算削减
    assert clock["t"] == 3                         # 只花在退避上(1s + 2s)


def test_repair_round_raising_still_reports_generate_round_usage(monkeypatch):
    """预审评审 #1 回归: 生成轮已计费 → 输出不可解析 → 修复轮抛错 → 生成轮的 usage/raw
    不得随栈帧一起丢掉。ISSUE-007 把这条从罕见路径变成了常见路径: 生成轮吃光链预算后,
    修复轮的 budget<=0 守卫立刻抛, 于是每次"慢且吐垃圾"的席都会漏账——正是 ISSUE-006/010
    声称已经关掉的那个洞, 且恰在出问题时漏得最多。"""
    clock = _fake_clock(monkeypatch)

    def prose_after_eating_budget(url, headers, payload, timeout):
        clock["t"] += timeout                      # 生成轮吃光本链预算
        return {"choices": [{"message": {"content": "这是散文不是 JSON"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2000, "completion_tokens": 500, "total_tokens": 2500}}

    monkeypatch.setattr(moa, "http_post", prose_after_eating_budget)
    member = {"name": "a", "seat": "A", "channel": "api", "model": "m1", "timeout_seconds": 100}
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 100, "max_tokens_member": 100})
    assert res["parsed"] is None
    assert res["err_class"] == "budget"            # 分类保持真实原因, 不被降级成 parse
    assert res["usage"]["total_tokens"] == 2500    # 已计费的 token 留在【逐席产物】里
    assert "散文" in res["raw"]                     # 原始输出留给人工抢救
    # 本用例钉的是"逐席产物不丢账"这一层; stats 侧的汇总由 ISSUE-012 的
    # test_stats_reports_wasted_spend_without_touching_existing_totals 钉。


def test_failure_record_names_the_link_that_actually_ran(monkeypatch):
    """预审评审 #2 回归: 失败席产物必须记【实际跑的那条链】的身份。把 fallback 链产出的
    raw/usage 挂在主通道 model 名下, 会直接打穿 ISSUE-008 的 roster.model_known ——
    synthesis.md 要求仲裁人「按 roster 逐席核对家族, 不要按 config 里写的 model 推断」,
    而那条路径上 roster 记的恰恰就是 config 的 model 且标成 model_known=true。
    channel_used 记 None 则是相对 v1.6.2 的直接回归(旧版这里是 'api')。"""

    def prose(cfg, system, user, temp, max_tokens, timeout, retries=2, **_):
        return f"prose, not JSON from {cfg['model']}", {"total_tokens": 10}

    monkeypatch.setattr(moa, "call_model", prose)
    member = {"name": "a", "seat": "A", "channel": "api", "protocol": "openrouter",
              "model": "openai/gpt-5.6-sol",
              "fallback": [{"channel": "api", "protocol": "openai",
                            "model": "anthropic/claude-opus-4.8"}]}
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 30, "max_tokens_member": 100})
    assert "anthropic/claude-opus-4.8" in res["raw"]          # 产物来自 fallback 链
    assert res["model_used"] == "anthropic/claude-opus-4.8"   # 身份必须跟着产物走
    assert res["channel_used"] and "fallback" in res["channel_used"]
    roster = moa.compute_stats("review", [res])["roster"][0]
    assert roster["model_used"] == "anthropic/claude-opus-4.8"


def test_budget_cut_prints_one_time_semantics_hint(monkeypatch, capsys):
    """v1.7.0 可发现性信号: 预算首次真砍掉一条链时到 stderr 说明一次语义变更 + 怎么调回去。
    ISSUE-007 是静默的用户可见行为变更(升级前靠重试熬到第 2 次才成功的席可能变成失败席),
    不读 CHANGELOG 的人需要当场看得懂。每进程只印一次,不刷屏。"""
    monkeypatch.setattr(moa, "_budget_hint_shown", False)
    clock = _fake_clock(monkeypatch)

    def burns_full_timeout(url, headers, payload, timeout):
        clock["t"] += timeout
        raise TimeoutError("simulated")

    monkeypatch.setattr(moa, "http_post", burns_full_timeout)
    member = {"name": "slow-seat", "seat": "A", "channel": "api", "model": "m1",
              "timeout_seconds": 100, "fallback": [{"channel": "api", "model": "m2"}]}
    opts = {"timeout_seconds": 100, "max_tokens_member": 100}
    moa._dispatch_channels(member, "r", "s", "u", opts)
    err = capsys.readouterr().err
    assert err.count("[budget]") == 1          # 两条链都被砍, 但只印一次
    assert "timeout_seconds" in err and "slow-seat" in err

    moa._dispatch_channels(member, "r", "s", "u", opts)
    assert "[budget]" not in capsys.readouterr().err   # 后续调用不再重复


def test_budget_banner_does_not_promise_a_fallback_that_does_not_exist(monkeypatch, capsys):
    """横幅在【最后一条链】上不得说"已让位给下一条 fallback"——该席根本没有下一条。
    用户照此去查降级链为何没生效,是在追一个不存在的现象。"""
    monkeypatch.setattr(moa, "_budget_hint_shown", False)
    clock = _fake_clock(monkeypatch)

    def burns_full_timeout(url, headers, payload, timeout):
        clock["t"] += timeout
        raise TimeoutError("simulated")

    monkeypatch.setattr(moa, "http_post", burns_full_timeout)
    solo = {"name": "solo-seat", "seat": "A", "channel": "api", "model": "m1",
            "timeout_seconds": 100}                      # 单链, 无 fallback
    moa._dispatch_channels(solo, "r", "s", "u", {"timeout_seconds": 100, "max_tokens_member": 100})
    err = capsys.readouterr().err
    assert "[budget]" in err and "solo-seat" in err
    assert "已让位给下一条 fallback" not in err           # 没有下一条可让
    assert "timeout_seconds" in err                      # 但"怎么调回去"仍要说清


def test_link_budget_skips_cli_repair_round_when_exhausted(monkeypatch):
    """cli 链首轮就用满预算且输出不可解析 → 不再开修复轮(那会再花一个 timeout),
    直接以 budget 类错误让位给下一条 fallback。"""
    clock = _fake_clock(monkeypatch)
    calls = []

    def slow_garbage(cfg, system, user, timeout):
        calls.append(cfg.get("model"))
        clock["t"] += timeout
        return "不是 JSON", None

    monkeypatch.setattr(moa, "call_cli_codex", slow_garbage)
    monkeypatch.setattr(moa, "call_with_json_repair",
                        lambda *a, **k: ("{}", {"verdict": "pass"}, {"total_tokens": 1}))
    member = {"name": "a", "seat": "A", "channel": "cli", "cli_kind": "codex", "model": "c1",
              "timeout_seconds": 100,
              "fallback": [{"channel": "api", "model": "m2"}]}
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 100, "max_tokens_member": 100})
    assert calls == ["c1"]                         # 只跑了首轮, 没有第二次 CLI 调用(修复轮)
    assert res["parsed"] == {"verdict": "pass"}    # 省下的时间让 api fallback 成功兜住


# ---------- 行为 4: 推理模型截断 → 重试倍增 max_tokens(修 OpenRouter gemini 空壳 bug) ----------
# 实测(2026-07,mem #10112/#10216): gemini-3.1-pro / gpt-5.6-sol 在 max_tokens 偏小时
# reasoning 吃光额度,content 返空壳且 finish_reason=length。旧代码按"空响应"原样重试
# (同 max_tokens)→ 确定性再失败,重试全浪费。修复:检测到截断,重试时倍增预算。

def test_truncated_empty_shell_retries_with_doubled_budget(monkeypatch):
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    budgets = []

    def reasoning_eats_budget(url, headers, payload, timeout):
        budgets.append(payload["max_tokens"])
        if payload["max_tokens"] < 6000:               # 预算不足 → 空壳
            return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        return {"choices": [{"message": {"content": '{"verdict":"pass"}'},
                             "finish_reason": "stop"}],
                "usage": {"total_tokens": 9}}

    monkeypatch.setattr(moa, "http_post", reasoning_eats_budget)
    content, usage = moa.call_model({"model": "m"}, "s", "u", 0.3, 3000, 30)
    assert budgets == [3000, 6000]                     # 空壳后预算倍增,而非原样重试
    assert '"verdict"' in content


def test_truncation_budget_capped_at_ceiling(monkeypatch):
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    budgets = []

    def always_empty_shell(url, headers, payload, timeout):
        budgets.append(payload["max_tokens"])
        return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}

    monkeypatch.setattr(moa, "http_post", always_empty_shell)
    with pytest.raises(moa.TransientError):
        moa.call_model({"model": "m"}, "s", "u", 0.3, 12000, 30)
    assert budgets == [12000, 16000, 16000]            # 封顶 _MAX_TOKENS_CEILING,不无限膨胀


def test_truncated_with_partial_content_returns_best_effort_on_last_attempt(monkeypatch):
    """所有重试后仍 finish_reason=length 但 content 非空 → 尽力返回(交给 parse/修复轮抢救),
    而非丢弃该席——旧行为直接返回首跑截断文本,新行为先重试大预算再兜底。"""
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    calls = {"n": 0}

    def always_truncated(url, headers, payload, timeout):
        calls["n"] += 1
        return {"choices": [{"message": {"content": '{"verdict":"pa'},
                             "finish_reason": "length"}],
                "usage": {"total_tokens": 5}}

    monkeypatch.setattr(moa, "http_post", always_truncated)
    content, usage = moa.call_model({"model": "m"}, "s", "u", 0.3, 3000, 30)
    assert calls["n"] == 3                             # 重试仍给满(首跑+2)
    assert content == '{"verdict":"pa'                 # 末次尽力返回截断内容


# ---------- 传输层: http_post 请求构造 + 响应解析(此前 0 覆盖,总被 stub 掉) ----------

def test_http_post_builds_post_request_and_parses_json(monkeypatch):
    """在 opener(urlopen)边界 stub,验证真实请求构造:POST / content-type / body / timeout 传参。"""
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": 1}'

    class FakeOpener:
        def open(self, req, timeout=None):
            captured.update(url=req.full_url, method=req.get_method(),
                            ct=req.headers.get("Content-type"), body=req.data, timeout=timeout)
            return FakeResp()

    monkeypatch.setattr(moa, "_opener_for", lambda url: FakeOpener())
    out = moa.http_post("https://x/v1/chat/completions", {"Authorization": "Bearer k"},
                        {"model": "m", "messages": []}, timeout=42)
    assert out == {"ok": 1}
    assert captured["method"] == "POST" and captured["ct"] == "application/json"
    assert captured["timeout"] == 42 and b'"model"' in captured["body"]


# ---------- ISSUE-012: 逐席 usage 累加器 ----------
# v1.7.0 曾加 wasted_tokens/wasted_members 又撤回,因为预审证明它两个方向【同时】错:
#   ① 最大的单个消耗点(call_model 截断重试,实测 3000→6000→12000 合计 21000 token)在重试
#      循环里就把 usage 丢了,报 0;
#   ② provider 省略 usage 时 _merge_usage({}) 产出全零【但为真】的 dict,使没花钱的席被计成花了。
# 根因是 usage 靠局部变量沿【正常返回路径】传递,于是每条异常路径都是丢弃点——逐点补丁修不完
# (第 1 轮补了 call_with_json_repair,第 3 轮就在它下面一层的 call_model 里找到同样的洞)。
# 正确修法:账本对象,每收到一个计费响应立刻记账,栈怎么展开都不影响已记的账。

def test_ledger_ignores_missing_and_all_zero_usage():
    """漏账方向②: provider 省略 usage 时不得记成一次计费调用。
    `{}` 与全零 dict 都不算花钱;任一字段为正才算。"""
    led = moa._UsageLedger()
    for u in (None, {}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
              "not a dict", {"total_tokens": None}):
        assert led.record(u) is False
    assert led.calls == 0 and led.as_dict()["total_tokens"] == 0
    assert led.record({"prompt_tokens": 10, "total_tokens": 10}) is True
    assert led.calls == 1


def test_ledger_keeps_billed_tokens_across_truncation_retries(monkeypatch):
    """漏账方向①: 截断重试的每一次 200 都【已计费】,但旧代码在循环里把 usage 丢了,
    最终 `raise last_err` 时整笔账消失。实测口径: 3000→6000→12000 三次调用合计 21000 token。"""
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))

    def always_truncated(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": payload["max_tokens"],
                          "total_tokens": 100 + payload["max_tokens"]}}

    monkeypatch.setattr(moa, "http_post", always_truncated)
    led = moa._UsageLedger()
    with pytest.raises(moa.TransientError):
        moa.call_model({"model": "m"}, "s", "u", 0.3, 3000, 30, ledger=led)
    assert led.calls == 3                                   # 三次都计了费
    assert led.as_dict()["completion_tokens"] == 3000 + 6000 + 12000
    assert led.as_dict()["total_tokens"] == 300 + 21000


def test_ledger_keeps_generate_round_when_repair_round_raises(monkeypatch):
    """生成轮已计费、修复轮抛错时,账不得随栈帧消失。旧代码靠把 usage 挂到异常上兜住
    (e.usage),那是逐点补丁;账本让它与栈展开无关。"""
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    seen = {"n": 0}

    def gen_ok_then_repair_dies(url, headers, payload, timeout):
        seen["n"] += 1
        if seen["n"] == 1:
            return {"choices": [{"message": {"content": "这不是 JSON"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 500}}
        raise moa.PermanentError("HTTP 401 auth", err_class="auth")

    monkeypatch.setattr(moa, "http_post", gen_ok_then_repair_dies)
    led = moa._UsageLedger()
    with pytest.raises(moa.PermanentError):
        moa.call_with_json_repair({"model": "m"}, "s", "u", 0.3, 100, 30, ledger=led)
    assert led.as_dict()["total_tokens"] == 500


def test_dispatch_exposes_seat_wide_ledger_across_fallback_links(monkeypatch):
    """账本是【逐席】的: 一席跑完失败链再降级成功时,usage_total 记全部花销,
    而 usage 仍只记【换回意见的那条链】——既有 token_usage 口径不得被改写(ISSUE-010 的取舍)。"""
    monkeypatch.setattr(moa.time, "sleep", lambda s: None)
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))

    def per_model(url, headers, payload, timeout):
        if payload["model"] == "m1":                        # 首链: 计费但输出不可解析
            return {"choices": [{"message": {"content": "垃圾"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 700}}
        return {"choices": [{"message": {"content": '{"verdict":"pass"}'},
                             "finish_reason": "stop"}], "usage": {"total_tokens": 40}}

    monkeypatch.setattr(moa, "http_post", per_model)
    member = {"name": "a", "seat": "A", "channel": "api", "model": "m1",
              "fallback": [{"channel": "api", "model": "m2"}]}
    res = moa._dispatch_channels(member, "r", "s", "u",
                                 {"timeout_seconds": 30, "max_tokens_member": 100})
    assert res["parsed"] == {"verdict": "pass"}
    assert res["usage"]["total_tokens"] == 40               # 既有口径: 成功那条链
    assert res["usage_total"]["total_tokens"] == 1440       # 700×2(生成+修复轮) + 40
    assert res["usage_total"]["calls"] == 3


def test_stats_reports_wasted_spend_without_touching_existing_totals():
    """白花的计费单列,【不并入】token_usage.total_tokens——后者是"换回了意见的成本",
    README 的成本倍数按这个口径读,改它等于悄悄改写既有结论(ISSUE-010)。"""
    def R(name, parsed, usage, usage_total):
        return {"name": name, "seat": name.upper(), "role": "r", "model_used": "m",
                "channel_used": "api", "parsed": parsed, "usage": usage,
                "usage_total": usage_total, "err_class": None, "error": None}
    results = [
        R("a", {"verdict": "pass", "issues": []}, {"total_tokens": 40},
          {"total_tokens": 1440, "calls": 3}),               # 成功, 但路上烧了 1400
        R("b", None, None, {"total_tokens": 900, "calls": 2}),  # 全败, 900 全白花
        R("c", None, None, {"total_tokens": 0, "calls": 0}),    # 订阅席/没花钱 → 不计
    ]
    st = moa.compute_stats("review", results)
    tu = st["token_usage"]
    assert tu["total_tokens"] == 40                          # 既有口径不变
    assert tu["billed_members"] == 1
    assert tu["wasted_tokens"] == 1400 + 900                 # a 的失败链 + b 的全部
    assert tu["wasted_members"] == 1                         # 只有 b: 计了费却没换回意见


# ---------- ISSUE-012 预审修复轮: 账本没铺到的三条路 ----------

def test_abandoned_straggler_keeps_its_ledger(monkeypatch):
    """弃席的账本不得随工作线程一起丢(预审 BLOCKER/HIGH2)。

    账本是 `_dispatch_channels` 的局部变量,活在 worker 线程的栈帧里;而弃席是
    `dispatch_with_quorum` 在【主线程】判定的,它只拿得到 member 配置。栈没有展开、账本还活着,
    只是没人拿得到 —— 于是一次真花了 18000 token 的运行会被 stats 主动断言成 wasted_tokens: 0。
    宽限窗正是为"慢 = 贵的重推理旗舰席"设计的,这不是边角场景。"""
    import threading
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    gate = threading.Event()

    def billed_then_stuck(url, headers, payload, timeout):
        if payload["model"] != "m-slow":
            return {"choices": [{"message": {"content": '{"verdict":"pass"}'},
                                 "finish_reason": "stop"}], "usage": {"total_tokens": 140}}
        if not gate.is_set():                 # 首次: 收到 200 并【已计费】, 但输出不可解析
            gate.set()
            return {"choices": [{"message": {"content": "垃圾"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2000, "completion_tokens": 16000,
                              "total_tokens": 18000}}
        release.wait(5)                       # 修复轮卡住 → 宽限窗到期被弃
        return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}

    release = threading.Event()
    monkeypatch.setattr(moa, "http_post", billed_then_stuck)
    members = [{"name": "fast1", "seat": "A", "channel": "api", "model": "m1"},
               {"name": "fast2", "seat": "B", "channel": "api", "model": "m2"},
               {"name": "flagship", "seat": "C", "channel": "api", "model": "m-slow"}]
    opts = {"timeout_seconds": 5, "max_tokens_member": 10}
    try:
        res = moa.dispatch_with_quorum(
            members, lambda m: moa._dispatch_channels(m, "r", "s", "u", opts),
            quorum_target=2, grace_s=0.05)
    finally:
        release.set()
    by = {r["name"]: r for r in res}
    assert by["flagship"]["err_class"] == "skipped_grace"
    assert "usage_total" in by["flagship"], "弃席产物缺 usage_total 键"
    assert by["flagship"]["usage_total"]["total_tokens"] == 18000   # 已计费的账不得报 0


def test_inject_and_turn_envelope_carry_usage_total_shape():
    """`--inject` 回填与讨论回合信封也要有 usage_total(零值即可): 账都是零不会算错数,
    但缺键会让"逐席产物都有 usage_total"成为假话,下游也没法按键存在性判断产物版本。"""
    m = {"name": "a", "seat": "A", "channel": "subagent"}
    inj = moa._inject_result(m, "review", {"verdict": "pass"})
    assert inj["usage_total"] == moa._UsageLedger().as_dict()
    env = moa._turn_envelope({**inj, "parsed": {"current_stance": "x"}}, 1)
    assert env["usage_total"] == moa._UsageLedger().as_dict()


def test_refine_stats_reports_wasted_spend_too():
    """精炼轮用同一份 roster 与同一条 fallback 链——生成轮会降级的席,精炼轮几乎必然再降级一次。
    `stats.r1.json` 不报 wasted_* 就让 SKILL.md「两个数都报」在那里无法执行(预审 HIGH3)。"""
    def R(name, parsed, usage, total):
        return {"name": name, "seat": name.upper(), "parsed": parsed, "usage": usage,
                "usage_total": {"total_tokens": total, "calls": 1}}
    prior = [R("a", {"verdict": "fail"}, None, 0), R("b", {"verdict": "fail"}, None, 0)]
    refine = [R("a", {"verdicts_on_others": [], "verdict": "fail"}, {"total_tokens": 140}, 1540),
              R("b", {"verdicts_on_others": [], "verdict": "fail"}, {"total_tokens": 140}, 140)]
    tu = moa.compute_refine_stats("review", prior, refine)["token_usage"]
    assert tu["total_tokens"] == 280          # 既有口径不变
    assert tu["wasted_tokens"] == 1400        # a 席在失败链上烧掉的


@pytest.mark.parametrize("usage,billed,expect_total", [
    ({"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}, 1, 140),
    ({"prompt_tokens": 50, "completion_tokens": 30}, 1, 0),      # 无 total: 仍算计费席
    ({"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 0}, 1, 0),
    ({"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": None}, 1, 0),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, 0, 0),  # 全零 = 没花钱
    ({}, 0, 0),
    (None, 0, 0),
])
def test_aggregate_usage_billed_filter(usage, billed, expect_total):
    """直接钉住 _aggregate_usage 的过滤条件(预审 HIGH4a: 改动零覆盖,改回旧写法 310 全绿)。
    判据与 _UsageLedger.record 一致——任一 token 字段为正即算计费,而不是只看 total_tokens,
    否则只给 prompt/completion 的端点(base_url 可配, vLLM/LiteLLM 等)会连已花的钱一起丢掉。"""
    r = {"name": "x", "seat": "A", "parsed": {"verdict": "pass"}, "usage": usage}
    agg = moa._aggregate_usage([r])
    assert agg["billed_members"] == billed
    assert agg["total_tokens"] == expect_total
    if billed and usage and usage.get("prompt_tokens"):
        assert agg["prompt_tokens"] == usage["prompt_tokens"]   # 已花的 p/c 不得被丢掉


def test_wasted_tokens_clamped_per_seat_not_on_the_sum():
    """`max(0, Σspent − Σbought)` 夹在总和上,一席的负贡献会抵消另一席的真实浪费。
    产生负贡献的输入是本仓库显式支持的: 手写 CH1 产物、collect-dir 里残留的 v1.8.0 产物
    都没有 usage_total 键(预审 MEDIUM5)。逐席算差、逐席夹 0,一席的口径异常不污染别席。"""
    old_artifact = {"name": "ch1", "seat": "B", "parsed": {"verdict": "pass"},
                    "usage": {"total_tokens": 5000}}          # 无 usage_total(v1.8.0 产物)
    burned = {"name": "dead", "seat": "C", "parsed": None, "usage": None,
              "usage_total": {"total_tokens": 4000, "calls": 2}}
    tu = moa.compute_stats("review", [old_artifact, burned])["token_usage"]
    assert tu["wasted_tokens"] == 4000        # 不被 ch1 席的负贡献抵消
    assert tu["wasted_members"] == 1


@pytest.mark.parametrize("v", [float("inf"), float("-inf"), float("nan"),
                               "inf", "Infinity", "nan", "-inf"])
def test_int_tokens_survives_non_finite(v):
    """json.loads 默认接受非标准的 Infinity/NaN 字面量,而 http_post 与 _read_artifact 都是裸
    json.loads。_int_tokens 在这些值上抛 OverflowError/ValueError,会把 stats 打成裸 traceback
    ——且是在全部委员 token 已经花完之后。同族的 _num/_str 都不抛(预审 MEDIUM6)。"""
    assert moa._int_tokens(v) == 0


def test_discuss_billed_calls_ignores_zero_usage():
    """billed_calls 仍用 dict 真值 —— 正是本版声称修掉的那个缺陷,只是换了个函数(预审 MEDIUM9)。"""
    turns = [{"round": 1, "seat": "A", "role": "r", "usage": {"total_tokens": 0},
              "turn": {"current_stance": "x", "responses": [], "new_argument": ""}},
             {"round": 1, "seat": "B", "role": "r", "usage": {"total_tokens": 50},
              "turn": {"current_stance": "y", "responses": [], "new_argument": ""}}]
    st = moa.compute_discuss_stats(turns, [])
    assert st["token_usage"]["billed_calls"] == 1      # 全零那次没花钱


# ---------- ISSUE-012 修复轮 2: 账本口径的最后四处不一致 ----------

def test_blindvote_artifact_carries_usage_total(tmp_path, monkeypatch):
    """收尾盲投是【真计费】的 CH3 调用,也会走 fallback 链,但 `bv` 信封是手搭的,漏了
    usage_total —— 同一场讨论里 discussion.jsonl 有账、blindvote_<seat>.json 没账(预审 D1)。
    它还是 CLAUDE.md collect-dir 接缝表里明列的三种产物之一。"""
    brief = tmp_path / "b.md"; brief.write_text("材料", encoding="utf-8")
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    monkeypatch.setattr(moa, "http_post", lambda *a, **k: {
        "choices": [{"message": {"content": '{"final_stance":"反对","confidence":0.8,'
                                            '"key_reason":"成本"}'}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 320}})
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "m"}],
           "options": {"max_tokens_member": 100, "timeout_seconds": 30}}
    args = types.SimpleNamespace(input=str(brief), member="a", collect_dir=str(tmp_path),
                                 mode="review", inject=None)
    moa.cmd_discuss_blindvote(args, cfg)
    bv = json.loads((tmp_path / "blindvote_A.json").read_text(encoding="utf-8"))
    assert "usage_total" in bv, "盲投产物缺 usage_total 键"
    assert bv["usage_total"]["total_tokens"] == 320


@pytest.mark.parametrize("usage,expect", [
    ({"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}, 140),
    ({"prompt_tokens": 50, "completion_tokens": 30}, 80),        # 无 total → 由 p+c 推
    ({"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 0}, 80),
    ({"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": None}, 80),
    ({"total_tokens": 140}, 140),
    ({}, 0), (None, 0),
])
def test_billed_total_is_the_single_source_for_all_four_judgements(usage, expect):
    """四处判据必须同源(预审 D4): `_has_billed_tokens` 管住了 billed_members 与 billed_calls,
    但 `_wasted_usage` 仍只看 total_tokens —— 于是只回 prompt/completion 的端点
    (vLLM / LiteLLM / 本地网关, 正是本版自己论证过的形状)上,一席会"被判计了费、却永远
    不可能被判白花",烧掉 8000 token 颗粒无收也报 0。"""
    assert moa._billed_total(usage) == expect
    assert moa._has_billed_tokens(usage) is (expect > 0)
    burned = {"name": "x", "seat": "A", "parsed": None, "usage": None,
              "usage_total": dict(usage or {}, calls=1)}
    tu = moa.compute_stats("review", [burned])["token_usage"]
    assert tu["wasted_tokens"] == expect
    assert tu["wasted_members"] == (1 if expect else 0)


def test_subscription_seat_self_reported_tokens_are_not_money():
    """缺 usage_total 时回落到自报 usage,对 v1.8.0 残留产物是对的(那时 usage 只在真计费时才有),
    但对【手写的 CH1 产物】是错的 —— CH1 走订阅,它自报的 token 不是钱。上一版在这里欠报 0,
    回落修法把它变成虚报 9999,方向翻了(预审 D3)。"一个两个方向都可能错的成本字段比没有更糟"
    是这个特性自己的历史教训。"""
    ch1_failed = {"name": "b", "seat": "B", "parsed": None, "protocol": "subagent",
                  "channel_used": "subagent (arbiter-dispatched)",
                  "usage": {"total_tokens": 9999}}          # CH1 自报, 免费
    tu = moa.compute_stats("review", [ch1_failed])["token_usage"]
    assert tu["wasted_tokens"] == 0 and tu["wasted_members"] == 0
    # 对照: 同样缺 usage_total 的【计费通道】失败产物, 那 80 确实是钱
    api_failed = {"name": "c", "seat": "C", "parsed": None, "protocol": "openrouter",
                  "channel_used": "api", "usage": {"total_tokens": 80}}
    tu2 = moa.compute_stats("review", [api_failed])["token_usage"]
    assert tu2["wasted_tokens"] == 80 and tu2["wasted_members"] == 1


def test_wasted_clamp_alone_handles_a_smaller_usage_total():
    """只针对【夹子】的用例(预审 D9): 产物同时有 usage 与一个【更小的】usage_total
    (手工编辑过),回落不生效,只有逐席夹能救它 —— 否则这一席产生负贡献去抵消别席。"""
    weird = {"name": "a", "seat": "A", "parsed": {"verdict": "pass"},
             "usage": {"total_tokens": 5000}, "usage_total": {"total_tokens": 40, "calls": 1}}
    burned = {"name": "b", "seat": "B", "parsed": None, "usage": None,
              "usage_total": {"total_tokens": 4000, "calls": 2}}
    tu = moa.compute_stats("review", [weird, burned])["token_usage"]
    assert tu["wasted_tokens"] == 4000          # 不被 a 席的 -4960 抵掉


@pytest.mark.parametrize("v,expect", [(True, 0), (False, 0), (-5, 0), (-1.9, 0), (2.9, 2)])
def test_int_tokens_bool_and_negative_guards(v, expect):
    """docstring 明确承诺的两条(bool 不是计数、负数夹 0)此前无用例(预审 D10)。"""
    assert moa._int_tokens(v) == expect


def test_merge_usage_survives_string_token_counts():
    """`_aggregate_usage` 这条路上,字符串 token 数会走到 _merge_usage 的 `int + str`
    而 TypeError —— stats 在全部委员 token 花完之后崩(预审 D7)。"""
    assert moa._merge_usage({"total_tokens": "140"}, {"total_tokens": 10})["total_tokens"] == 150
    r = {"name": "x", "seat": "A", "parsed": {"verdict": "pass"}, "usage": {"total_tokens": "140"}}
    assert moa.compute_stats("review", [r])["token_usage"]["total_tokens"] == 140


def test_seat_ledger_does_not_leak_between_rounds(monkeypatch):
    """跨轮串账的防线(dispatch_with_quorum 入口清表)此前无回归网 —— 变异实测删掉它 329 全绿
    (预审 D5c)。生成轮弃席后,精炼轮的同名席不得继承上一轮的账。"""
    monkeypatch.setattr(moa, "endpoint_and_headers", lambda cfg: ("http://x", {}))
    moa._SEAT_LEDGERS.clear()
    moa._SEAT_LEDGERS["solo"] = led = moa._UsageLedger()
    led.record({"total_tokens": 7777})                        # 上一轮遗留的账
    members = [{"name": "solo", "seat": "A"}]
    res = moa.dispatch_with_quorum(
        members, lambda m: moa._fail(m, "r", "boom", "transient"), quorum_target=1, grace_s=0)
    assert res[0]["usage_total"]["total_tokens"] == 0         # 不得继承 7777


def test_skipped_grace_takes_its_ledger_explicitly():
    """`_skipped_grace` 直接读模块全局会让结果取决于测试执行顺序(预审 D5b: 已经在
    test_skipped_grace_record_has_usage_key 上发生了)。改成显式收参,直调即确定。"""
    moa._SEAT_LEDGERS.clear()
    moa._SEAT_LEDGERS["a"] = poisoned = moa._UsageLedger()
    poisoned.record({"total_tokens": 5000})
    r = moa._skipped_grace({"name": "a", "seat": "A"})         # 不传账本 → 记零, 不读全局
    assert r["usage_total"]["total_tokens"] == 0
    led = moa._UsageLedger(); led.record({"total_tokens": 42})
    assert moa._skipped_grace({"name": "a"}, led)["usage_total"]["total_tokens"] == 42


def test_seat_ledger_clear_at_round_start_is_load_bearing(monkeypatch):
    """上一条清表用例其实不区分: 它的 fn 不经过 _dispatch_channels(不发布也不覆盖), 该席又
    立刻正常完成、从不进弃席分支,断言成立靠的是 _fail 自带的零账本(预审 R5)。
    清表只在一个窗口里起作用——【残留条目存在 + 该席被弃 + 它的 worker 还没来得及发布】。
    这里构造那个窗口:solo 在被弃前一直卡住,绝不进 _dispatch_channels。"""
    import threading
    gate = threading.Event()
    moa._SEAT_LEDGERS.clear()
    moa._SEAT_LEDGERS["solo"] = stale = moa._UsageLedger()
    stale.record({"total_tokens": 7777})              # 上一轮遗留

    def fn(m):
        if m["name"] == "solo":
            gate.wait(5)                              # 被弃之前不进 _dispatch_channels
        return {"name": m["name"], "seat": m.get("seat"), "role": "r",
                "parsed": {"verdict": "pass"}, "usage": None,
                "usage_total": moa._UsageLedger().as_dict(),
                "latency_s": 0.0, "error": None, "err_class": None}

    members = [{"name": "fast", "seat": "A"}, {"name": "solo", "seat": "B"}]
    try:
        res = moa.dispatch_with_quorum(members, fn, quorum_target=1, grace_s=0)
    finally:
        gate.set()
    by = {r["name"]: r for r in res}
    assert by["solo"]["err_class"] == "skipped_grace"
    assert by["solo"]["usage_total"]["total_tokens"] == 0     # 无清表则为 7777


@pytest.mark.parametrize("result,expect", [
    # 判不出通道的产物按【未知】处理: 手写 CH1 产物自报的 token 不是钱, 不得算成白花
    ({"name": "x", "seat": "A", "parsed": None, "usage": {"total_tokens": 9999}}, 0),
    # 声明了计费通道的旧产物: 回落到自报值, 且走 _billed_total(只给 p/c 也算得出来)
    ({"name": "x", "seat": "A", "parsed": None, "channel_used": "api",
      "usage": {"prompt_tokens": 500, "completion_tokens": 200}}, 700),
])
def test_wasted_fallback_needs_a_declared_billed_channel(result, expect):
    """缺 usage_total 时的回落只在产物【自己声明了通道】时生效,且走 _billed_total。
    这两行是修复轮自己写的,此前无测试覆盖(预审 round-3 Q2/Q3)。"""
    assert moa._wasted_usage([result])["wasted_tokens"] == expect


@pytest.mark.parametrize("channel_used,is_sub", [
    ("cli:auggie", False),          # auggie = Augment 按上游价 +40% 结算, 是【计费】通道
    ("cli:auggie (fallback from channel=cli)", False),
    ("cli:codex", True),            # codex 走订阅
    ("subagent (arbiter-dispatched)", True),
    ("api", False),
])
def test_is_subscription_seat_leaves_auggie_billed(channel_used, is_sub):
    """把判据从 `cli:codex` 放宽成 `cli:` 会【静默】把每个 auggie 席的白花归零,
    而出厂默认阵容有三个 auggie 席(A/C/D)。此前无测试(预审 round-3 Q5)。"""
    assert moa._is_subscription_seat({"channel_used": channel_used}) is is_sub
