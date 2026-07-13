"""CH2 auggie CLI 通道单测(spec: tasks/specs/auggie-cli-channel.md)。

覆盖四块:
  1. call_cli_auggie 命令构造(--output-format json 信封 / --instruction-file 不走 argv /
     空 workspace 防索引注入)与错误分类(startup/timeout/auth/cli/empty)
  2. resolve_channel 的 cli_kind 解析:显式 codex/auggie;auto=检测到 auggie 优先、
     codex 殿后;auto 的 auggie try 不继承 codex 专属 cli_extra、模型只取 auggie_model
  3. _effective_billing:auggie 计费(Augment 上游价+40%)记 billed,codex 订阅记 sub
  4. _dispatch_channels cli 路径:JSON 解析失败给一次 CLI 修复轮(对齐 api 路径),
     channel_used 标注实走 kind(cli:auggie / cli:codex)

故障注入边界与 test_fault_injection.py 相同:只 stub subprocess.run / _which,
分类、信封解析、修复、调度全走真代码路径。
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import moa  # noqa: E402


def _proc(rc, out=b"", err=b""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _envelope(result, is_error=False):
    """auggie --print --output-format json 的真实信封形状(0.32.0 实测)。"""
    return json.dumps({"type": "result", "result": result, "is_error": is_error,
                       "subtype": "success", "session_id": "s", "num_turns": 1,
                       "request_id": "req-1"}).encode("utf-8")


# ---------- 1. call_cli_auggie: 命令构造 + 错误分类 ----------

def test_auggie_missing_binary_is_permanent(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: None)
    with pytest.raises(moa.PermanentError) as ei:
        moa.call_cli_auggie({"auggie_bin": "nope"}, "s", "u", 5)
    assert ei.value.err_class == "startup"


def test_auggie_timeout_is_transient(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")

    def boom(*a, **k):
        raise moa.subprocess.TimeoutExpired(cmd="auggie", timeout=1)

    monkeypatch.setattr(moa.subprocess, "run", boom)
    with pytest.raises(moa.TransientError) as ei:
        moa.call_cli_auggie({}, "s", "u", 1)
    assert ei.value.err_class == "timeout"


def test_auggie_auth_error_is_permanent(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(1, err=b"Not authenticated. Run `auggie login`"))
    with pytest.raises(moa.PermanentError) as ei:
        moa.call_cli_auggie({}, "s", "u", 5)
    assert ei.value.err_class == "auth"


def test_auggie_generic_nonzero_is_transient(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(2, err=b"upstream hiccup"))
    with pytest.raises(moa.TransientError):
        moa.call_cli_auggie({}, "s", "u", 5)


def test_auggie_envelope_is_error_is_transient(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(0, out=_envelope("server exploded", is_error=True)))
    with pytest.raises(moa.TransientError):
        moa.call_cli_auggie({}, "s", "u", 5)


def test_auggie_empty_result_is_transient(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(0, out=_envelope("   ")))
    with pytest.raises(moa.TransientError) as ei:
        moa.call_cli_auggie({}, "s", "u", 5)
    assert ei.value.err_class == "empty"


def test_auggie_success_parses_fenced_json_from_envelope(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    monkeypatch.setattr(moa.subprocess, "run",
                        lambda *a, **k: _proc(0, out=_envelope('```json\n{"verdict":"pass"}\n```')))
    raw, parsed = moa.call_cli_auggie({"model": "haiku4.5"}, "s", "u", 5)
    assert parsed == {"verdict": "pass"}


def test_auggie_non_envelope_stdout_strips_request_id_trailer(monkeypatch):
    """信封解析失败(旧版/异常输出)时回退纯文本,剥掉尾部 Request ID 行再解析。"""
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    out = b'{"verdict":"pass"}\n\n\nRequest ID: 73a905ee-1850-48ef-8e21-85c4c770280c\n'
    monkeypatch.setattr(moa.subprocess, "run", lambda *a, **k: _proc(0, out=out))
    raw, parsed = moa.call_cli_auggie({}, "s", "u", 5)
    assert parsed == {"verdict": "pass"}


def test_auggie_command_shape_prompt_not_in_argv(monkeypatch):
    """核心安全/正确性约束一次断言:prompt 走 --instruction-file(不进 argv)、
    --workspace-root 指向空目录(防索引注入)、--output-format json、--max-turns 1、
    --dont-save-session、--model 透传、cli_extra 追加。"""
    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        i = cmd.index("--instruction-file")
        seen["prompt"] = Path(cmd[i + 1]).read_text(encoding="utf-8")
        ws = Path(cmd[cmd.index("--workspace-root") + 1])
        seen["ws_empty"] = ws.is_dir() and not any(ws.iterdir())
        return _proc(0, out=_envelope('{"ok":1}'))

    monkeypatch.setattr(moa.subprocess, "run", fake_run)
    moa.call_cli_auggie({"model": "gpt5.6-sol", "cli_extra": ["--persona", "x"]},
                        "SYS<秘密>", "USR<材料>", 30)
    cmd = seen["cmd"]
    assert "--print" in cmd and "--quiet" in cmd and "--dont-save-session" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--model") + 1] == "gpt5.6-sol"
    assert "--persona" in cmd                        # cli_extra 追加
    assert seen["ws_empty"]                          # workspace = 空目录
    assert "SYS<秘密>" in seen["prompt"] and "USR<材料>" in seen["prompt"]
    assert not any("SYS<秘密>" in c for c in cmd)    # prompt 不进 argv


# ---------- 2. resolve_channel: cli_kind 解析 ----------

def _tries(member):
    return moa.resolve_channel(member)


def test_explicit_cli_kind_auggie(monkeypatch):
    tries = _tries({"channel": "cli", "cli_kind": "auggie", "model": "gpt5.6-sol"})
    assert len(tries) == 1
    kind, cfg, _ = tries[0]
    assert kind == "cli" and cfg["cli_kind"] == "auggie" and cfg["model"] == "gpt5.6-sol"


def test_explicit_cli_kind_codex_keeps_legacy_behavior(monkeypatch):
    tries = _tries({"channel": "cli", "cli_kind": "codex", "cli_extra": ["-c", "x=y"]})
    assert len(tries) == 1
    kind, cfg, _ = tries[0]
    assert kind == "cli" and cfg["cli_kind"] == "codex" and cfg["cli_extra"] == ["-c", "x=y"]


def test_auto_prefers_auggie_then_codex(monkeypatch):
    """检测到 auggie → auggie 优先、codex 殿后(用户指令:稳定+模型全)。"""
    monkeypatch.setattr(moa, "_which", lambda e: f"/usr/bin/{e}")   # 两个都在 PATH
    tries = _tries({"channel": "cli", "model": "gpt-5-codex",
                    "auggie_model": "gpt5.6-sol", "cli_extra": ["-c", "eff=high"]})
    kinds = [(k, c["cli_kind"]) for k, c, _ in tries]
    assert kinds == [("cli", "auggie"), ("cli", "codex")]
    aug, cod = tries[0][1], tries[1][1]
    # auggie try: 模型只取 auggie_model(两侧模型 ID 命名空间不同),不继承 codex 专属 cli_extra
    assert aug["model"] == "gpt5.6-sol" and not aug.get("cli_extra")
    # codex try: 保持旧语义
    assert cod["model"] == "gpt-5-codex" and cod["cli_extra"] == ["-c", "eff=high"]


def test_auto_without_auggie_falls_back_to_codex_only(monkeypatch):
    monkeypatch.setattr(moa, "_which",
                        lambda e: "/usr/bin/codex" if e == "codex" else None)
    tries = _tries({"channel": "cli"})
    assert [(k, c["cli_kind"]) for k, c, _ in tries] == [("cli", "codex")]


def test_auto_neither_binary_still_yields_codex_try(monkeypatch):
    """两个二进制都不在 → 仍给 codex try,让 startup 错误浮出(而非静默 0 通道)。"""
    monkeypatch.setattr(moa, "_which", lambda e: None)
    tries = _tries({"channel": "cli"})
    assert len(tries) == 1 and tries[0][1]["cli_kind"] == "codex"


def test_fallback_cli_entry_also_expands(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: f"/usr/bin/{e}")
    tries = _tries({"channel": "api", "model": "m",
                    "fallback": [{"channel": "cli", "cli_kind": "auggie", "model": "kimi-k2.7"}]})
    assert tries[0][0] == "api"
    assert tries[1][0] == "cli" and tries[1][1]["cli_kind"] == "auggie" \
        and tries[1][1]["model"] == "kimi-k2.7"


def test_validate_config_rejects_bad_cli_kind():
    cfg = {"members": [{"name": "a", "channel": "cli", "cli_kind": "gemini"}],
           "options": {}}
    with pytest.raises(SystemExit):
        moa.validate_config(cfg)


# ---------- 3. 计费判定: auggie=billed(上游价+40%), codex=sub ----------

def test_billing_auggie_is_billed(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: f"/usr/bin/{e}")
    assert moa._effective_billing({"channel": "cli", "cli_kind": "auggie"}) == "billed"


def test_billing_auto_with_auggie_present_is_billed(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: f"/usr/bin/{e}")
    assert moa._effective_billing({"channel": "cli"}) == "billed"


def test_billing_explicit_codex_stays_sub(monkeypatch):
    monkeypatch.setattr(moa, "_which", lambda e: f"/usr/bin/{e}")
    assert moa._effective_billing({"channel": "cli", "cli_kind": "codex"}) == "sub"


# ---------- 4. _dispatch_channels cli 路径: 修复轮 + channel_used 标注 ----------

def _opts():
    return {"timeout_seconds": 30, "max_tokens_member": 100}


def test_cli_parse_fail_gets_one_repair_round(monkeypatch):
    """实测 5 跑 2 跑输出含未转义引号 → 解析失败。cli 路径应与 api 对齐:
    花一次 CLI 修复调用自愈,而非直接把该席丢给 fallback。"""
    member = {"name": "a", "seat": "A", "channel": "cli", "cli_kind": "auggie"}
    calls = {"n": 0}

    def fake_auggie(ccfg, system, user, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"broken": "未转义"引号"}', None          # 首跑坏 JSON
        assert "不是合法 JSON" in system                      # 修复轮提示词
        return '{"verdict":"pass"}', {"verdict": "pass"}

    monkeypatch.setattr(moa, "call_cli_auggie", fake_auggie)
    res = moa._dispatch_channels(member, "r", "s", "u", _opts())
    assert calls["n"] == 2
    assert res["parsed"] == {"verdict": "pass"}
    assert res["channel_used"].startswith("cli:auggie")


def test_cli_repair_also_fails_falls_to_next_channel(monkeypatch):
    member = {"name": "a", "seat": "A", "channel": "cli", "cli_kind": "auggie",
              "fallback": [{"channel": "api", "protocol": "openrouter", "model": "backup"}]}

    monkeypatch.setattr(moa, "call_cli_auggie", lambda *a, **k: ("still broken", None))
    monkeypatch.setattr(moa, "call_with_json_repair",
                        lambda *a, **k: ("{}", {"verdict": "pass"}, {"total_tokens": 1}))
    res = moa._dispatch_channels(member, "r", "s", "u", _opts())
    assert res["parsed"] == {"verdict": "pass"}
    assert "fallback" in res["channel_used"]


def test_dispatch_routes_kind_codex_to_codex_fn(monkeypatch):
    member = {"name": "a", "seat": "A", "channel": "cli", "cli_kind": "codex"}
    monkeypatch.setattr(moa, "call_cli_codex",
                        lambda *a, **k: ('{"verdict":"pass"}', {"verdict": "pass"}))
    res = moa._dispatch_channels(member, "r", "s", "u", _opts())
    assert res["parsed"] == {"verdict": "pass"}
    assert res["channel_used"].startswith("cli:codex")
