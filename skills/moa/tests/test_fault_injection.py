"""委员故障注入 E2E(requirements §12.5)。

三种故障 → 三种正确行为,各在真实 moa.py 函数上跑通(故障只在传输边界 http_post /
call_model 注入,重试/退避/分类/修复/中止全走真代码路径):
  1. 瞬态/超时  → call_model 指数退避重试(永久错误不重试)
  2. 非法 JSON  → call_with_json_repair 花一次修复调用自愈(合法则不浪费调用)
  3. 全体委员挂 → dispatch 返回 0 成功 → cmd_generate 的 min_ok 门中止

「全挂→中止」另有一条真实 API E2E(坏模型 ID → 全 404 → abort),见 moa-reports/e2e-fault/。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import moa  # noqa: E402


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

    def fake_call_model(cfg, system, user, temp, max_tokens, timeout, retries=2):
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

    def fake_call_model(cfg, system, user, temp, max_tokens, timeout, retries=2):
        calls["n"] += 1
        return '{"verdict":"pass"}', {"total_tokens": 3}

    monkeypatch.setattr(moa, "call_model", fake_call_model)
    raw, parsed, usage = moa.call_with_json_repair({"model": "m"}, "s", "u", 0.3, 100, 30)
    assert calls["n"] == 1                    # 一次成功就不浪费修复调用
    assert parsed == {"verdict": "pass"}


def test_repair_also_fails_returns_none(monkeypatch):
    def fake_call_model(cfg, system, user, temp, max_tokens, timeout, retries=2):
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
