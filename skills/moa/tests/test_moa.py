"""moa.py 离线测试套件(无网络)。

运行: cd skills/moa && python -m pytest tests/ -q
覆盖: 配置/角色解析、代理判定、错误分类、JSON 修复、统计块、通道调度、
      fallback 展开、Quorum 宽限窗、endpoint 构造。真实 API/CLI 调用不在此(见 E2E)。
"""
import io
import json
import os
import sys
import threading
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
    # 顶层是合法 JSON 但非对象(数组/标量/bool/null): 委员响应 schema 一律是对象,
    # 非对象不是有效响应。单对象数组 → 抠出该对象;多对象/标量 → 判失败(None),交修复轮/计失败。
    ('[{"a":1}]', {"a": 1}),            # 模型把响应包成单元素数组 → 恢复出对象
    ('true', None),
    ('42', None),
    ('"just a string"', None),
    ('[1,2,3]', None),                  # 纯标量数组,无对象可抠
])
def test_parse_json(text, expect):
    assert moa.parse_json(text) == expect


def test_stats_tolerates_non_object_member_output(tmp_path):
    """委员输出为「合法但非对象」JSON(如数组/标量)时,stats 不得崩溃,应把该席计为 failed。
    回归 ISSUE-001: 旧代码 parse_json 返回 list/bool,被 compute_stats 当 dict 调 .get() → AttributeError。"""
    good = {"name": "ok-a", "seat": "A", "role": "feasibility_skeptic", "model_used": "m",
            "channel_used": "api", "raw": "{}",
            "parsed": {"verdict": "pass", "confidence": 0.8, "issues": []},
            "usage": None, "latency_s": 1.0, "error": None, "err_class": None}
    bad = {"name": "bad-b", "seat": "B", "role": "maintainability_reviewer", "model_used": "m",
           "channel_used": "api", "raw": "[...]",
           "parsed": ["not", "an", "object"],   # 已落盘的非对象 parsed(历史产物 / 手工注入)
           "usage": None, "latency_s": 1.0, "error": None, "err_class": None}
    (tmp_path / "member_ok-a.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "member_bad-b.json").write_text(json.dumps(bad), encoding="utf-8")
    stats = moa.compute_stats("review", [good, bad])
    assert stats["members_ok"] == 1        # 只有 ok-a 算成功
    assert stats["members_failed"] == 1    # 非对象的 bad-b 计入 failed
    assert stats["degraded"] is True


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
    # fp 必须是真实可读对象: classify_http_error 的 4xx 分支会 e.read() 取 hint;
    # Python 3.9 下 fp=None 会走 tempfile 路径 KeyError('file')(3.12 恰好宽容)——给 BytesIO 两版一致。
    return urllib.error.HTTPError("http://x", code, "msg", {}, io.BytesIO(b"msg"))


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


def test_resolve_channel_cli_with_api_fallback(monkeypatch):
    """裸 channel:cli 默认 cli_kind=auto: 检测到 auggie 则展开为 auggie→codex 两个 try,
    再接 api fallback(v1.4.0 契约;显式 cli_kind 单 try 见 test_auggie_channel.py)。"""
    monkeypatch.setattr(moa, "_which", lambda e: f"/usr/bin/{e}")   # 两个二进制都在,密封环境差异
    m = {"name": "x", "channel": "cli", "model": "gpt",
         "fallback": [{"channel": "api", "protocol": "openrouter", "model": "openai/gpt"}]}
    tries = moa.resolve_channel(m)
    assert [t[0] for t in tries] == ["cli", "cli", "api"]
    assert [t[1].get("cli_kind") for t in tries[:2]] == ["auggie", "codex"]
    # fallback 合并了 member 基础字段
    _, cfg, note = tries[2]
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


def test_effective_billing_matches_actual_run(monkeypatch):
    """dry-run 计费判定须与 moa.py 真正会跑的通道一致(回归 dry-run 少报 bug):
    旧逻辑只看主通道,把'subagent + api fallback'误记为免费订阅,而 generate 实际走计费 API。
    v1.4.0: auggie 计费(上游价+40%)记 billed;本例钉住"只有 codex 在 PATH"以密封环境差异,
    auggie 在场的计费判定见 test_auggie_channel.py。"""
    monkeypatch.setattr(moa, "_which",
                        lambda e: "/usr/bin/codex" if e == "codex" else None)
    # 纯 subagent(无 api/cli fallback)= 仲裁人免费派发
    assert moa._effective_billing({"channel": "subagent", "model": "claude"}) == "sub"
    # subagent + api fallback = 脚本实跑计费 API(旧逻辑误记为免费,回归点)
    assert moa._effective_billing(
        {"channel": "subagent", "model": "claude",
         "fallback": [{"channel": "api", "model": "anthropic/claude"}]}) == "billed"
    # subagent + cli fallback(实解析为 codex)= 订阅免费
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
    member = {"name": "skeptic-a", "seat": "A", "channel": "cli", "cli_kind": "codex",
              "protocol": "codex"}            # 无 model;显式 codex(密封 auto 的环境探测)
    opts = {"timeout_seconds": 60, "max_tokens_member": 100}
    res = moa._dispatch_channels(member, "feasibility_skeptic", "sys", "usr", opts)
    assert res["parsed"] == {"verdict": "pass"}
    assert res["model_used"] is None          # 省 model → None,非崩溃
    assert res["channel_used"] == "cli:codex"  # v1.4.0: 标注实走 kind
    assert res["err_class"] is None


# ---------- P1-1: config 最小 schema 校验(缺字段指名报错,非裸 KeyError) ----------

@pytest.mark.parametrize("cfg", [
    {"members": [{"name": "a", "channel": "api"}]},          # 缺 options
    {"options": {}},                                          # 缺 members
    {"members": [], "options": {}},                           # members 空
    {"members": [{"channel": "api"}], "options": {}},         # member 缺 name
    {"members": [{"name": "x", "channel": "bogus"}], "options": {}},  # channel 非法
    {"members": [{"name": "x"}, {"name": "x"}], "options": {}},       # name 重复(会互相覆盖)
    {"members": [{"name": "a/b"}, {"name": "a_b"}], "options": {}},   # 规范化后碰撞(→同一文件名)
    # grace_seconds 校验(v1.6.1): 非数值 → dispatch `now+v` 裸 TypeError; 负值 → 窗立即过期静默秒弃席
    {"members": [{"name": "x", "grace_seconds": "150"}], "options": {}},  # 按席 非数值(YAML 引号化)
    {"members": [{"name": "x", "grace_seconds": -5}], "options": {}},     # 按席 负值(手误 → 反效果)
    {"members": [{"name": "x", "grace_seconds": True}], "options": {}},   # 按席 bool(非秒数语义)
    {"members": [{"name": "x"}], "options": {"grace_seconds": "90"}},     # 全局 非数值
    {"members": [{"name": "x"}], "options": {"grace_seconds": -1}},       # 全局 负值
])
def test_validate_config_rejects_broken(cfg):
    with pytest.raises(SystemExit):
        moa.validate_config(cfg)


def test_validate_config_accepts_valid():
    moa.validate_config({"members": [{"name": "a", "channel": "api"},
                                     {"name": "b", "channel": "subagent"},
                                     {"name": "c"}],  # channel 省略默认 api
                         "options": {"max_tokens_member": 100}})


def test_validate_config_accepts_valid_grace():
    """grace_seconds 合法值: 未设 / int / float / 0 均放行(全局与按席)。"""
    moa.validate_config({"members": [{"name": "a", "grace_seconds": 150},      # int
                                     {"name": "b", "grace_seconds": 90.0},     # float
                                     {"name": "c", "grace_seconds": 0},        # 0 = 无宽限, 合法
                                     {"name": "d"}],                           # 未设 = 用默认
                         "options": {"grace_seconds": 90}})


# ---------- F5: auto cli_kind + model 无 auggie_model → 告警(不阻断) ----------

def test_validate_config_warns_auto_cli_model_without_auggie_model(capsys):
    """F5: channel=cli + auto(默认)+ 设了 model 但无 auggie_model → 打告警
    (auggie 优先且只认 auggie_model,member.model 会被静默顶替)。不阻断(不抛 SystemExit)。"""
    moa.validate_config({"members": [{"name": "a", "channel": "cli", "model": "gpt5.6-sol"}],
                         "options": {}})
    err = capsys.readouterr().err
    assert "auggie_model" in err and "顶替" in err


def test_validate_config_no_warn_when_auggie_model_present(capsys):
    """显式 auggie_model(或显式 cli_kind)→ 无 F5 告警。"""
    moa.validate_config({"members": [
        {"name": "a", "channel": "cli", "model": "x", "auggie_model": "gpt5.6-sol"},
        {"name": "b", "channel": "cli", "cli_kind": "codex", "model": "y"},  # 显式 kind → 无告警
    ], "options": {}})
    assert "顶替" not in capsys.readouterr().err


# ---------- F2: cmd_refine 全席精炼失败 → 非零退出(本轮零产出) ----------

def test_cmd_refine_aborts_when_all_fail(tmp_path, monkeypatch):
    brief = tmp_path / "b.md"; brief.write_text("brief", encoding="utf-8")
    collect = tmp_path / "out"; collect.mkdir()
    # 上一轮产物(round 0)存在,供精炼读取 own_prior
    prior = {"name": "a", "seat": "A", "role": "r", "parsed": {"verdict": "fail"}}
    (collect / "member_a.json").write_text(json.dumps(prior), encoding="utf-8")
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "m"}],
           "options": {"timeout_seconds": 60, "max_tokens_member": 100,
                       "min_successful_members": 1, "grace_seconds": 0}}
    monkeypatch.setattr(moa, "run_member_refine",
                        lambda *a, **k: moa._fail({"name": "a", "seat": "A"}, "r", "boom", "transient"))
    args = types.SimpleNamespace(input=str(brief), member=None, collect_dir=str(collect),
                                 mode="review", round=1)
    with pytest.raises(SystemExit):
        moa.cmd_refine(args, cfg)


# ---------- F6: dry-run 对"首选订阅 + 计费 fallback"席提示降级转计费 ----------

def test_dry_run_flags_sub_first_with_billed_fallback(capsys):
    """F6: cli:codex(订阅,首 try)挂 api fallback → 提示降级会转计费。"""
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "cli", "cli_kind": "codex",
                        "model": "gpt", "fallback": [{"channel": "api", "model": "m"}]}],
           "options": {}}
    moa.dry_run(cfg, "review", "material", "", 0)
    assert "fallback 含计费通道" in capsys.readouterr().out


def test_fallback_has_billed():
    assert moa._fallback_has_billed(
        {"channel": "cli", "cli_kind": "codex",
         "fallback": [{"channel": "api", "model": "m"}]}) is True
    assert moa._fallback_has_billed({"channel": "subagent", "model": "c"}) is False


# ---------- P1-2: refine/discuss 禁止静默回退示例配置 ----------

def test_resolve_config_refuses_example_fallback(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(SystemExit):
        moa.resolve_config(str(missing), allow_example_fallback=False)


def test_resolve_config_allows_example_fallback_for_generate(tmp_path):
    missing = tmp_path / "nope.yaml"
    cfg = moa.resolve_config(str(missing), allow_example_fallback=True)
    assert isinstance(cfg, dict) and cfg.get("members")  # 回退到 assets/config.example.yaml


# ---------- P1-4: brainstorm 默认高温发散; 显式温度优先 ----------

def _capture_temp(monkeypatch):
    seen = {}
    def fake_repair(cfg, system, user, temp, max_tokens, timeout, schema=None):
        seen["temp"] = temp
        return '{"ideas":[]}', {"ideas": []}, {}
    monkeypatch.setattr(moa, "call_with_json_repair", fake_repair)
    return seen


def test_brainstorm_defaults_high_temp_review_low(monkeypatch):
    seen = _capture_temp(monkeypatch)
    member = {"name": "radical-a", "seat": "A", "channel": "api", "model": "m"}
    opts = {"timeout_seconds": 60, "max_tokens_member": 100}
    moa.run_member_generate(member, "brainstorm", "material", "topic", opts, {})
    assert seen["temp"] == 0.9          # 发散
    moa.run_member_generate(member, "review", "material", "", opts, {})
    assert seen["temp"] == 0.3          # 稳定判断


def test_explicit_temperature_overrides_mode_default(monkeypatch):
    seen = _capture_temp(monkeypatch)
    member = {"name": "r", "seat": "A", "channel": "api", "model": "m",
              "temperature_generate": 0.1}
    opts = {"timeout_seconds": 60, "max_tokens_member": 100}
    moa.run_member_generate(member, "brainstorm", "m", "t", opts, {})
    assert seen["temp"] == 0.1          # member 显式设置优先于模式默认


# ---------- C2: write_member 文件名 sanitize(防路径穿越) ----------

def test_safe_name_strips_traversal():
    assert "/" not in moa._safe_name("../../etc/passwd")
    assert ".." not in moa._safe_name("../evil")
    assert "/" not in moa._safe_name("a/b/c")


def test_safe_name_preserves_normal_names():
    assert moa._safe_name("skeptic-a") == "skeptic-a"
    assert moa._safe_name("custom_b.1") == "custom_b.1"


def test_write_member_stays_inside_collect_dir(tmp_path):
    p = moa.write_member(tmp_path, {"name": "../evil", "parsed": {"ok": 1}})
    assert p.parent == tmp_path                 # 没被 ../ 写出目录
    assert ".." not in p.name and "/" not in p.name
    assert p.exists()


# ---------- C4: _bypass_proxy 支持 NO_PROXY=* 通配 ----------

def test_bypass_proxy_wildcard(monkeypatch):
    monkeypatch.setenv("no_proxy", "*")         # 小写键;代码优先读 no_proxy
    assert moa._bypass_proxy("openrouter.ai") is True
    assert moa._bypass_proxy("any.host.example") is True


# ---------- 入口层: discuss-turn --inject 非法 JSON → 退出(不静默污染 transcript) ----------

def test_discuss_turn_bad_inject_json_exits(tmp_path):
    brief = tmp_path / "b.md"; brief.write_text("brief", encoding="utf-8")
    bad = tmp_path / "bad.json"; bad.write_text("definitely not json", encoding="utf-8")
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "subagent"}],
           "options": {"timeout_seconds": 60, "max_tokens_member": 100}}
    args = types.SimpleNamespace(input=str(brief), member="a", inject=str(bad),
                                 collect_dir=str(tmp_path / "out"), mode="decide", round=1)
    with pytest.raises(SystemExit):
        moa.cmd_discuss_turn(args, cfg)


# ---------- 入口层: _select_members --member 子集过滤 ----------

def test_select_members_filters_by_name():
    cfg = {"members": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
    assert [m["name"] for m in moa._select_members(cfg, "a,c")] == ["a", "c"]
    assert len(moa._select_members(cfg, None)) == 3          # 无过滤 → 全体


def test_select_members_no_match_exits():
    with pytest.raises(SystemExit):
        moa._select_members({"members": [{"name": "a"}]}, "zzz")


# ---------- 入口层: cmd_generate 成功席 < min_ok → 中止(顾问不足不配称委员会) ----------

def test_cmd_generate_aborts_below_min_ok(tmp_path, monkeypatch):
    brief = tmp_path / "b.md"; brief.write_text("brief", encoding="utf-8")
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "m"},
                       {"name": "b", "seat": "B", "channel": "api", "model": "m"}],
           "options": {"timeout_seconds": 60, "max_tokens_member": 100,
                       "min_successful_members": 2, "grace_seconds": 0}}
    monkeypatch.setattr(moa, "run_member_generate",
                        lambda m, *a: moa._fail(m, "r", "boom", "transient"))  # 全挂
    args = types.SimpleNamespace(input=str(brief), member=None,
                                 collect_dir=str(tmp_path / "out"), mode="review", topic="")
    with pytest.raises(SystemExit):
        moa.cmd_generate(args, cfg)


def test_cmd_generate_min_ok_scoped_to_dispatchable_not_all_members(tmp_path, monkeypatch):
    """N1 回归: min_ok 分母是【可派发席】,不是全体席位。默认配置形态(2 纯 subagent + 2 可派发,
    min_successful_members=2)下,两个可派发席都成功即达标——不得因'含 subagent 的全体=4、ok=2<门'
    之类的错口径中止。旧 bug: min_ok=min(2,len(members)=4)=2,quorum_target=max(2,len(dispatchable)-1=1)=2,
    可派发席掉一个→ok=1<2 被误 abort;修后分母=len(dispatchable)=2,掉一个仍 ok=1... 见下一个用例。"""
    brief = tmp_path / "b.md"; brief.write_text("brief", encoding="utf-8")
    cfg = {"members": [{"name": "sub-b", "seat": "B", "channel": "subagent", "model": "m"},
                       {"name": "sub-d", "seat": "D", "channel": "subagent", "model": "m"},
                       {"name": "api-a", "seat": "A", "channel": "api", "model": "m"},
                       {"name": "api-c", "seat": "C", "channel": "api", "model": "m"}],
           "options": {"timeout_seconds": 60, "max_tokens_member": 100,
                       "min_successful_members": 2, "grace_seconds": 0}}
    monkeypatch.setattr(moa, "run_member_generate",
                        lambda m, *a: {"name": m["name"], "seat": m["seat"], "role": "r",
                                       "model_used": "m", "channel_used": "api", "raw": "{}",
                                       "parsed": {"verdict": "pass"}, "usage": None,
                                       "latency_s": 0.0, "error": None, "err_class": None})
    args = types.SimpleNamespace(input=str(brief), member=None,
                                 collect_dir=str(tmp_path / "out"), mode="review", topic="")
    moa.cmd_generate(args, cfg)  # 不得抛 SystemExit: 两个可派发席成功即达标
    # 只有两个可派发席落盘(subagent 席交仲裁人,moa.py 跳过)
    written = sorted(p.name for p in (tmp_path / "out").glob("member_*.json"))
    assert written == ["member_api-a.json", "member_api-c.json"]


def test_cmd_generate_all_subagent_exits_clean_not_abort(tmp_path, capsys):
    """N1 回归: 全 CH1 配置(无可派发席)干净返回,不以'顾问不足'abort。"""
    brief = tmp_path / "b.md"; brief.write_text("brief", encoding="utf-8")
    cfg = {"members": [{"name": "sub-a", "seat": "A", "channel": "subagent", "model": "m"},
                       {"name": "sub-b", "seat": "B", "channel": "subagent", "model": "m"}],
           "options": {"timeout_seconds": 60, "max_tokens_member": 100,
                       "min_successful_members": 2, "grace_seconds": 0}}
    args = types.SimpleNamespace(input=str(brief), member=None,
                                 collect_dir=str(tmp_path / "out"), mode="review", topic="")
    moa.cmd_generate(args, cfg)  # 不抛 SystemExit
    assert "all seats are channel=subagent" in capsys.readouterr().err


def test_cmd_generate_still_aborts_when_dispatchable_below_min_ok(tmp_path, monkeypatch):
    """N1 反向: 分母改了但 abort 门仍有效——可派发席不足 min_ok 时依旧中止。
    2 可派发席、min_successful_members=2、只有 1 席成功 → ok=1<min_ok=min(2,2)=2 → abort。"""
    brief = tmp_path / "b.md"; brief.write_text("brief", encoding="utf-8")
    cfg = {"members": [{"name": "api-a", "seat": "A", "channel": "api", "model": "m"},
                       {"name": "api-c", "seat": "C", "channel": "api", "model": "m"}],
           "options": {"timeout_seconds": 60, "max_tokens_member": 100,
                       "min_successful_members": 2, "grace_seconds": 0}}

    def one_ok_one_fail(m, *a):
        if m["name"] == "api-a":
            return {"name": m["name"], "seat": m["seat"], "role": "r", "model_used": "m",
                    "channel_used": "api", "raw": "{}", "parsed": {"verdict": "pass"},
                    "usage": None, "latency_s": 0.0, "error": None, "err_class": None}
        return moa._fail(m, "r", "boom", "transient")

    monkeypatch.setattr(moa, "run_member_generate", one_ok_one_fail)
    args = types.SimpleNamespace(input=str(brief), member=None,
                                 collect_dir=str(tmp_path / "out"), mode="review", topic="")
    with pytest.raises(SystemExit):
        moa.cmd_generate(args, cfg)


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


def test_dispatch_grace_returns_without_joining_straggler():
    """P0-1 回归: 宽限到期必须【立即返回】, 不 join 落伍线程。
    旧 `with ThreadPoolExecutor` 实现块退出隐式 shutdown(wait=True) 会 join 全部线程,
    使 wall≈落伍者时长、宽限窗形同虚设。此测断言 wall 远小于落伍者阻塞时长。"""
    release = threading.Event()
    members = [{"name": "fast1", "seat": "A"}, {"name": "fast2", "seat": "B"},
               {"name": "slow", "seat": "C"}]

    def fn(m):
        if m["name"] == "slow":
            release.wait(timeout=5.0)  # 阻塞直到测试放行, 模拟落伍者
        return {"name": m["name"], "seat": m["seat"], "parsed": {"ok": 1},
                "role": "r", "channel_used": "api", "latency_s": 0.0,
                "model_used": "m", "err_class": None, "error": None}

    t0 = time.monotonic()
    res = moa.dispatch_with_quorum(members, fn, quorum_target=2, grace_s=0.2)
    elapsed = time.monotonic() - t0
    release.set()  # 放行落伍线程, 避免拖累后续用例/进程退出
    assert elapsed < 1.5, f"宽限到期未立即返回 (wall={elapsed:.1f}s) — 疑似又在 join 落伍线程"
    by = {r["name"]: r for r in res}
    assert by["fast1"]["parsed"] and by["fast2"]["parsed"]
    assert by["slow"]["err_class"] == "skipped_grace" and by["slow"]["parsed"] is None


def test_dispatch_member_grace_override_survives_while_default_skips():
    """按席 grace override(v1.6.0): 同一轮两个落伍席——slowKept 带 member 级
    grace_seconds 大窗应【存活】(在全局小窗下本会被牺牲); slowDrop 不带 override,
    按全局小窗被 skipped_grace。证按席宽限生效 + 未设 override 的默认行为不变。"""
    members = [{"name": "fast1", "seat": "A"}, {"name": "fast2", "seat": "B"},
               {"name": "slowKept", "seat": "C", "grace_seconds": 2.0},
               {"name": "slowDrop", "seat": "D"}]  # 无 override → 用全局 grace_s

    def fn(m):
        if m["name"] == "slowKept":
            time.sleep(0.4)     # < 自身 2.0s 窗 → 应完成
        elif m["name"] == "slowDrop":
            time.sleep(3.0)     # >> 全局 0.1s 窗 → 应被 skip
        return {"name": m["name"], "seat": m["seat"], "parsed": {"ok": 1},
                "role": "r", "channel_used": "api", "latency_s": 0.0,
                "model_used": "m", "err_class": None, "error": None}

    t0 = time.monotonic()
    res = moa.dispatch_with_quorum(members, fn, quorum_target=2, grace_s=0.1)
    elapsed = time.monotonic() - t0
    by = {r["name"]: r for r in res}
    # 高价值慢席用自身大窗存活
    assert by["slowKept"]["parsed"] and by["slowKept"]["err_class"] is None
    # 未设 override 的落伍席仍按全局小窗被牺牲(默认不变)
    assert by["slowDrop"]["err_class"] == "skipped_grace" and by["slowDrop"]["parsed"] is None
    # slowDrop 的 3.0s 阻塞不得拖累返回(其窗 0.1s 到期即弃, slowKept 0.4s 完成)
    assert elapsed < 1.5, f"按席窗未独立生效 (wall={elapsed:.1f}s)"


def test_dispatch_member_grace_zero_skips_immediately_under_large_global():
    """按席 grace_seconds=0: 达法定数即刻弃该落伍席, 不受全局大窗影响(反向覆盖: 按席窗
    确实压过全局)。全局 grace_s=10 本会等很久, 但该席自设 0 → 秒弃 → 函数迅速返回。"""
    members = [{"name": "fast1", "seat": "A"}, {"name": "fast2", "seat": "B"},
               {"name": "noWait", "seat": "C", "grace_seconds": 0}]

    def fn(m):
        if m["name"] == "noWait":
            time.sleep(3.0)     # 慢, 但自身 0 窗 → 达标即弃, 不等它
        return {"name": m["name"], "seat": m["seat"], "parsed": {"ok": 1},
                "role": "r", "channel_used": "api", "latency_s": 0.0,
                "model_used": "m", "err_class": None, "error": None}

    t0 = time.monotonic()
    res = moa.dispatch_with_quorum(members, fn, quorum_target=2, grace_s=10.0)
    elapsed = time.monotonic() - t0
    by = {r["name"]: r for r in res}
    assert by["noWait"]["err_class"] == "skipped_grace" and by["noWait"]["parsed"] is None
    assert elapsed < 1.0, f"按席 0 窗未压过全局大窗 (wall={elapsed:.1f}s)"


# ---------- main() argparse 接线冒烟(此前 0 覆盖) ----------

def test_main_stats_routes_without_config(tmp_path, monkeypatch, capsys):
    """接线: stats 走免-config 特例分支(main() 不加载委员会 config)。"""
    (tmp_path / "member_a.json").write_text(
        json.dumps({"name": "a", "seat": "A", "model_used": "m", "channel_used": "api",
                    "parsed": {"verdict": "pass", "confidence": 0.5, "issues": []}}),
        encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["moa.py", "stats", "--mode", "review", "--collect-dir", str(tmp_path)])
    moa.main()                                            # 不因缺 config 抛错
    assert '"members_ok": 1' in capsys.readouterr().out


def test_main_leak_check_routes_without_config(tmp_path, monkeypatch):
    """接线: leak-check 走免-config 分支;空目录 → 0 文件 → 退出码 2。"""
    monkeypatch.setattr(sys, "argv", ["moa.py", "leak-check", str(tmp_path)])
    with pytest.raises(SystemExit) as ei:
        moa.main()
    assert ei.value.code == 2


def test_main_refine_forbids_example_fallback(tmp_path, monkeypatch):
    """接线(P1-2 不变量的调用点): refine 在 no_fallback 集合内 → 无 config.yaml 时禁止回退示例配置。"""
    brief = tmp_path / "b.md"; brief.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                           # cwd 无 config.yaml
    monkeypatch.setattr(sys, "argv",
                        ["moa.py", "refine", "--input", str(brief),
                         "--collect-dir", str(tmp_path), "--round", "1"])
    with pytest.raises(SystemExit) as ei:
        moa.main()
    assert "禁止回退" in str(ei.value)


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
    # 上一轮多数 = fail(3 fail vs 1 pass,genuine majority——修 F4 后平票不再当多数派,
    # 故基准 fixture 必须是真多数)。本轮 d 无理由(无 challenge)从 pass 翻向 fail → 谄媚。
    prior = [_rf("a", "fail", []), _rf("b", "fail", []),
             _rf("c", "fail", []), _rf("d", "pass", [])]
    refine = [
        _rf("a", "fail", []),
        _rf("b", "fail", []),
        _rf("c", "fail", []),
        _rf("d", "fail", []),   # pass->fail 翻向上一轮多数派 fail,且未提 challenge
    ]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["sycophancy_detail"]["prior_majority_verdict"] == "fail"
    assert s["sycophancy_alert"] is True
    assert s["sycophancy_detail"]["movers"] == 1
    assert s["sycophancy_detail"]["flips_toward_majority"] == 1


def test_refine_stats_challenge_is_not_sycophancy():
    # b 翻向多数(fail),但提出了 challenge(有新证据代理)→ 不算谄媚。
    # prior 用真多数(2 fail vs 1 pass),使"challenge 豁免"路径而非平票 None 成为判否的原因。
    prior = [_rf("a", "fail", []), _rf("b", "pass", []), _rf("c", "fail", [])]
    refine = [
        _rf("a", "fail", []),
        _rf("b", "fail", [{"ref_title": "X", "stance": "challenge", "reason": "r"}]),
        _rf("c", "fail", []),
    ]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["sycophancy_detail"]["prior_majority_verdict"] == "fail"
    assert s["sycophancy_alert"] is False


def test_majority_verdict_tie_returns_none():
    """修 F4: 最高票并列 → None(无多数派),不让 dict 插入序决定基准;清晰多数正常返回。"""
    assert moa._majority_verdict([_rf("a", "fail", []), _rf("b", "pass", [])], "verdict") is None
    assert moa._majority_verdict(
        [_rf("a", "fail", []), _rf("b", "fail", []), _rf("c", "pass", [])], "verdict") == "fail"
    assert moa._majority_verdict([], "verdict") is None


def test_refine_stats_no_early_stop_when_seat_failed():
    """修 F3: 本轮有席位失败 → 即便成功席 verdict 全一致也不建议早停(证据不全,幸存者偏差)。"""
    prior = [_rf("a", "fail", []), _rf("b", "fail", [])]
    refine = [_rf("a", "fail", []),
              {"name": "b", "seat": "A", "parsed": None, "err_class": "server", "error": "x"}]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["round_members_failed"] == 1
    assert s["early_stop_suggested"] is False      # 全一致但有失败席 → 不早停


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
