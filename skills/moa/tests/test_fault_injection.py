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

    def fake_repair(ccfg, system, user, temp, max_tokens, timeout, schema):
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
