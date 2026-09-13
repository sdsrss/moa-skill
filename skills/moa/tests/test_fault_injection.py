"""委员故障注入 E2E(requirements §12.5)。

三种故障 → 三种正确行为,各在真实 moa.py 函数上跑通(故障只在传输边界 http_post /
call_model 注入,重试/退避/分类/修复/中止全走真代码路径):
  1. 瞬态/超时  → call_model 指数退避重试(永久错误不重试)
  2. 非法 JSON  → call_with_json_repair 花一次修复调用自愈(合法则不浪费调用)
  3. 全体委员挂 → dispatch 返回 0 成功 → cmd_generate 的 min_ok 门中止

「全挂→中止」另有一条真实 API E2E(坏模型 ID → 全 404 → abort),见 moa-reports/e2e-fault/。
"""
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
    # 注: 不断言 stats 汇总——wasted_* 已撤回(见 test_stats_token_usage_counts_successful_seats_only),
    # 本用例钉的是"逐席产物不丢账", 那是后续累加器重构的输入。


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
