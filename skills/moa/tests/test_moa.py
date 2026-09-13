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
import yaml

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


def _mk(parsed):
    return {"name": "x", "seat": "A", "role": "r", "model_used": "m",
            "channel_used": "api", "parsed": parsed, "usage": None}


@pytest.mark.parametrize("parsed", [
    {"verdict": "pass", "confidence": "high", "issues": []},        # confidence 非数字字符串
    {"verdict": "pass", "confidence": "0.8", "issues": []},         # 数字字符串
    {"verdict": "pass", "confidence": 0.7, "issues": "无问题"},      # issues 写成字符串
    {"verdict": "pass", "confidence": 0.7, "issues": ["缺测试"]},    # issues 为字符串数组
    {"verdict": ["pass"], "confidence": 0.7, "issues": []},         # verdict 非字符串(不可哈希风险)
    {"verdict": "pass", "confidence": 0.7,
     "issues": [{"severity": ["high"]}]},                          # severity 非字符串
])
def test_compute_stats_review_malformed_fields_no_crash(parsed):
    """ISSUE-002: 单席 review 嵌套字段类型错乱不得让 compute_stats 崩栈。"""
    stats = moa.compute_stats("review", [_mk(parsed)])
    assert stats["members_ok"] == 1        # parsed 是对象 → 仍算成功席,只是字段被容错
    assert isinstance(stats["issue_count_by_severity"], dict)


@pytest.mark.parametrize("mode,parsed", [
    ("decide", {"claimed_option": "A", "confidence": "high",
                "opponent_fatal_flaws": ["bad"], "spike_suggestion": ["x"]}),
    ("brainstorm", {"ideas": ["idea one", "idea two"]}),
    ("brainstorm", {"ideas": "just a string"}),
])
def test_compute_stats_decide_brainstorm_malformed_no_crash(mode, parsed):
    """ISSUE-002: decide/brainstorm 嵌套字段类型错乱不得崩栈。"""
    stats = moa.compute_stats(mode, [_mk(parsed)])
    assert stats["members_ok"] == 1


def test_compute_discuss_stats_malformed_responses_no_crash():
    """ISSUE-002: 讨论回合 responses/new_argument 类型错乱不得让 discuss-stats 崩栈。"""
    ts = [{"round": 1, "seat": "A", "role": "r",
           "turn": {"still_holding": "x", "current_stance": "y",
                    "responses": "agree with all",       # 应为对象数组,却是字符串
                    "new_argument": ["not", "a", "string"],
                    "position_changed": False}}]
    stats = moa.compute_discuss_stats(ts, [])
    assert stats["turns_ok"] == 1
    assert isinstance(stats["dissent_preserved"], list)


@pytest.mark.parametrize("seats", [
    [1, "A"],          # config 里 seat: 1 与 seat: A 混用 → sorted() 在 str/int 间比较崩栈
    [["A"], "B"],      # 手工编辑 jsonl 给出不可哈希 seat → last_by_seat 字典键崩
    [None, "B"],       # seat 缺失
])
def test_compute_discuss_stats_odd_seat_types_no_crash(seats):
    """seat 在讨论聚合里同时当字典键与排序键: 非字符串 seat 不得让 discuss-stats 崩栈。
    合法配置(全字符串 seat)的结果必须与规约前一致——见下方 participants 断言。"""
    ts = [{"round": 1, "seat": s, "role": "r",
           "turn": {"still_holding": "x", "current_stance": "y",
                    "responses": [], "new_argument": "n"}} for s in seats]
    stats = moa.compute_discuss_stats(ts, [])
    assert stats["turns_ok"] == len(seats)
    assert all(isinstance(p, str) for p in stats["participants"])


def test_compute_discuss_stats_string_seats_unchanged():
    """规约不得改变合法配置的读数: 全字符串 seat 时 participants/drift 与旧行为一致。"""
    ts = [{"round": 1, "seat": s, "role": "r",
           "turn": {"still_holding": "x", "current_stance": s, "responses": [],
                    "new_argument": "n"}} for s in ("B", "A")]
    bv = [{"seat": "A", "vote": {"final_stance": "A-blind"}}]
    stats = moa.compute_discuss_stats(ts, bv)
    assert stats["participants"] == ["A", "B"]
    pair = [p for p in stats["blind_vote_drift_pairs"] if p["seat"] == "A"][0]
    assert pair["discussion_final"] == "A" and pair["blind_final"] == "A-blind"


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
    # 数值型选项校验(ISSUE-003): 与 grace 同源,手误引号化/非法值会裸 TypeError
    {"members": [{"name": "x"}], "options": {"min_successful_members": "2"}},  # 全局 非数值(会整轮崩栈)
    {"members": [{"name": "x"}], "options": {"min_successful_members": -1}},   # 负值
    {"members": [{"name": "x"}], "options": {"timeout_seconds": "180"}},       # 全局 非数值
    {"members": [{"name": "x"}], "options": {"timeout_seconds": 0}},           # 0 超时=立即失败,无效
    {"members": [{"name": "x"}], "options": {"max_tokens_member": "3000"}},    # 非数值
    {"members": [{"name": "x"}], "options": {"max_tokens_member": 0}},         # 0 token 无效
    {"members": [{"name": "x", "timeout_seconds": "5"}], "options": {}},       # 按席 timeout 非数值
    {"members": [{"name": "x", "timeout_seconds": -5}], "options": {}},        # 按席 timeout 负值
    # seat 写空: YAML `seat:` 即 None, 会一路漏到 load_role_prompt 的 re.escape(None) 才崩(裸 traceback)
    {"members": [{"name": "x", "seat": None}], "options": {}},                 # YAML `seat:` (null)
    {"members": [{"name": "x", "seat": ""}], "options": {}},                   # 空串
    {"members": [{"name": "x", "seat": "  "}], "options": {}},                 # 全空白
])
def test_validate_config_rejects_broken(cfg):
    with pytest.raises(SystemExit):
        moa.validate_config(cfg)


def test_read_input_missing_file_named_error(tmp_path):
    """ISSUE-005: --input 指向不存在文件 → 具名 SystemExit,而非裸 FileNotFoundError traceback。"""
    with pytest.raises(SystemExit) as ei:
        moa._read_input(str(tmp_path / "nope.md"))
    assert "[input]" in str(ei.value)


def test_read_input_reads_existing(tmp_path):
    f = tmp_path / "brief.md"
    f.write_text("hello 简报", encoding="utf-8")
    assert moa._read_input(str(f)) == "hello 简报"


def test_read_inject_missing_file_named_error(tmp_path):
    with pytest.raises(SystemExit) as ei:
        moa._read_inject(str(tmp_path / "nope.json"))
    assert "[inject]" in str(ei.value)


# ---------- stats --mode 与产物不符: 静默给出全零共识读数 ----------

def _art(name, parsed):
    return {"name": name, "seat": "A", "role": "r", "model_used": "m", "channel_used": "api",
            "raw": "", "parsed": parsed, "usage": None, "latency_s": 1.0,
            "error": None, "err_class": None}


def _stats_args(tmp_path, mode, round_no=0):
    return types.SimpleNamespace(collect_dir=str(tmp_path), mode=mode, round=round_no)


def test_stats_rejects_mode_mismatch(tmp_path):
    """generate --mode brainstorm 后 stats 忘了带 --mode(默认 review)→ 旧行为静默产出
    verdict_tally={'?':1} / mean_confidence=0.0 的全零读数。而 SKILL.md 第 4 步要求仲裁人
    「报告中涉及数量与共识度的表述必须与 stats 一致,不得凭印象改写」——静默的错读数会被
    照抄进最终报告。必须 fail-fast 并指出正确命令。"""
    (tmp_path / "member_a.json").write_text(json.dumps(
        _art("a", {"ideas": [{"title": "点子", "novelty": 5, "feasibility": 3}]})), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        moa.cmd_stats(_stats_args(tmp_path, "review"), None)
    assert "--mode brainstorm" in str(ei.value)


def test_stats_accepts_matching_mode(tmp_path, capsys):
    (tmp_path / "member_a.json").write_text(json.dumps(
        _art("a", {"ideas": [{"title": "点子", "novelty": 5, "feasibility": 3}]})), encoding="utf-8")
    moa.cmd_stats(_stats_args(tmp_path, "brainstorm"), None)
    assert '"total_ideas_before_dedup": 1' in capsys.readouterr().out


@pytest.mark.parametrize("parsed", [
    None,                                    # 该席失败, 无形状可判
    {},                                      # 空对象
    {"summary": "只写了总结"},                # 非任何 mode 的判据键
    {"ideas": [], "verdict": "pass"},        # 两个 mode 的键同时出现 → 歧义
])
def test_stats_mode_check_silent_when_shape_is_ambiguous(tmp_path, parsed, capsys):
    """防误拒(v1.7.1 A1 的教训): 形状判不出来时保持沉默照常聚合,绝不拦下合法工作流。"""
    (tmp_path / "member_a.json").write_text(json.dumps(_art("a", parsed)), encoding="utf-8")
    moa.cmd_stats(_stats_args(tmp_path, "review"), None)
    assert '"members_ok"' in capsys.readouterr().out


def test_stats_mode_check_survives_degraded_run(tmp_path):
    """降级运行(部分席失败)在本项目里是常态。失败席没有形状可判,但不该有否决权——
    否则这道门恰好在最该起作用的场景上失效。"""
    (tmp_path / "member_a.json").write_text(json.dumps(
        _art("a", {"ideas": [{"title": "点子", "novelty": 5}]})), encoding="utf-8")
    dead = {**_art("b", None), "err_class": "server", "error": "boom"}
    (tmp_path / "member_b.json").write_text(json.dumps(dead), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        moa.cmd_stats(_stats_args(tmp_path, "review"), None)
    assert "--mode brainstorm" in str(ei.value)


def test_stats_mode_mismatch_also_checked_on_refine_round(tmp_path):
    """精炼轮同一陷阱: decide 精炼产物用 --mode review 聚合 → stance_tally 全零。"""
    (tmp_path / "member_a.json").write_text(json.dumps(
        _art("a", {"claimed_option": "PG"})), encoding="utf-8")
    (tmp_path / "member_a.r1.json").write_text(json.dumps(
        _art("a", {"cross_exam": [], "revised_claimed_option": "PG"})), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        moa.cmd_stats(_stats_args(tmp_path, "review", round_no=1), None)
    assert "--mode decide" in str(ei.value)


# ---------- collect-dir 接缝: CH1 席产物由仲裁人手写,坏文件要具名报错 ----------

@pytest.mark.parametrize("body", [
    '{"name":"a","parsed":{"verdict":"pass"},}',   # 尾逗号(手写最常见)
    '{"name":"a", "parsed": ',                     # 截断写入
    '[{"name":"a"}]',                              # 顶层不是对象
    '{"seat":"C","parsed":{"verdict":"pass"}}',    # 漏了 name(聚合层的席位主键)
])
def test_load_members_named_error_on_bad_artifact(tmp_path, body):
    """member_*.json 不全是 moa.py 写的: CH1 子代理席由仲裁人按格式手写落盘(collect-dir 接缝)。
    手写就会有尾逗号/漏字段,旧行为是 JSONDecodeError / KeyError 冒到顶,连是哪个文件都不说。
    对齐 ISSUE-005 给 --input/--inject 的口径: 具名 SystemExit + 指出文件。"""
    (tmp_path / "member_a.json").write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        moa.load_members(tmp_path)
    assert "[collect]" in str(ei.value) and "member_a.json" in str(ei.value)


@pytest.mark.parametrize("body", [
    "members:\n  - name: a\n   seat: A\n    channel: api\noptions: {}\n",   # 缩进错位
    "members:\n\t- name: a\noptions: {}\n",                                  # tab 缩进
    "members: [{name: a}\noptions: {}\n",                                    # 括号没闭合
])
def test_resolve_config_named_error_on_malformed_yaml(tmp_path, body):
    """手改 config.yaml 的缩进/tab 是最高频的用户错误,旧行为是 yaml 库的 ScannerError 裸抛。
    resolve_config 已经给【文件不存在】具名报错了,【文件坏了】却没有——同一道门两种待遇。"""
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        moa.resolve_config(str(p))
    assert "[config]" in str(ei.value) and str(p) in str(ei.value)


@pytest.mark.parametrize("fname,call", [
    ("brief.md", lambda moa_, p: moa_._read_input(str(p))),
    ("inj.json", lambda moa_, p: moa_._read_inject(str(p))),
    ("config.yaml", lambda moa_, p: moa_.resolve_config(str(p))),
    ("member_a.json", lambda moa_, p: moa_.load_members(p.parent)),
])
def test_non_utf8_file_gives_named_error(tmp_path, fname, call):
    """非 UTF-8 文件(Windows 下 GBK 存的中文简报 / 配置)在中文项目里很常见,旧行为是裸
    UnicodeDecodeError。四扇读文件的门口径要一致——leak_check 的 _iter_text_files 早就
    `except (UnicodeDecodeError, OSError)` 了,只有这几扇漏着。"""
    p = tmp_path / fname
    p.write_bytes("委员名: 评审甲\nmembers:\n  - name: a\n".encode("gbk"))
    with pytest.raises(SystemExit) as ei:
        call(moa, p)
    assert "UTF-8" in str(ei.value)          # 报错要点名编码,而不是只说"读取失败"


def test_resolve_config_named_error_when_path_is_a_directory(tmp_path):
    with pytest.raises(SystemExit) as ei:
        moa.resolve_config(str(tmp_path))
    assert "[config]" in str(ei.value)


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="需要 POSIX 字符设备")
def test_resolve_config_reads_non_regular_file(capsys):
    """`--config <(…)`(进程替换 → /dev/fd/N)与 `/dev/stdin` 给的是 FIFO / 字符设备,
    `is_file()` 为假。若按"不存在"处理,generate/dry-run 会【静默】换成出厂 4 席示例委员会并
    真花钱,refine/discuss 则报一句"文件不存在"的假话 —— 正是 resolve_config docstring 里
    P1-2 要防的那件事(预审 H2)。`--input` 一直用 exists(),两扇门口径也不该不一致。

    用 /dev/null 而非 FIFO: 它同属"存在但非常规文件",却不需要并发写端。先前的 FIFO 版本要
    另起线程写入,而读端在写端已关闭后再 open 会永久阻塞 —— 实测 20% 的整套运行被挂死,
    且在修复前的代码上同样挂,危害来自测试本身(预审 B1)。测试不该比被测缺陷更危险。"""
    assert Path("/dev/null").exists() and not Path("/dev/null").is_file()
    cfg = moa.resolve_config("/dev/null")          # 空 YAML → None,但【不得】走示例回退
    assert cfg is None
    assert "using assets/config.example.yaml" not in capsys.readouterr().err


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root 绕过目录权限位,chmod 0500 拦不住 mkdir(容器 CI 常以 root 跑)")
def test_ensure_collect_dir_named_error_when_unwritable(tmp_path):
    """--collect-dir 落在不可写位置(路径手误 / 只读挂载)→ 旧行为裸 PermissionError。
    每条命令都收这个参数,是最常被敲错的路径之一。"""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(SystemExit) as ei:
            moa._ensure_collect_dir(ro / "sub")
        assert "[collect-dir]" in str(ei.value)
    finally:
        ro.chmod(0o700)


def test_ensure_collect_dir_creates_and_is_idempotent(tmp_path):
    d = tmp_path / "a" / "b"
    assert moa._ensure_collect_dir(d) == d and d.is_dir()
    assert moa._ensure_collect_dir(d) == d        # 已存在 → 不报错


def test_discuss_stats_named_error_on_bad_blindvote(tmp_path):
    """blindvote_*.json 同为手写可达路径(--inject 回填 CH1 盲投),坏文件同样要具名报错。"""
    (tmp_path / "discussion.jsonl").write_text(
        json.dumps({"round": 1, "seat": "A", "role": "r",
                    "turn": {"current_stance": "s", "responses": [], "new_argument": ""}}) + "\n",
        encoding="utf-8")
    (tmp_path / "blindvote_A.json").write_text('{"seat":"A", broken', encoding="utf-8")
    args = types.SimpleNamespace(collect_dir=str(tmp_path))
    with pytest.raises(SystemExit) as ei:
        moa.cmd_discuss_stats(args, None)
    assert "[collect]" in str(ei.value) and "blindvote_A.json" in str(ei.value)


def test_validate_config_accepts_valid_numeric_options():
    """数值选项合法值: 未设 / 正数 / min_successful_members=0 均放行(ISSUE-003)。"""
    moa.validate_config({"members": [{"name": "a", "timeout_seconds": 240},   # 按席正数
                                     {"name": "b"}],                          # 未设 = 用默认
                         "options": {"min_successful_members": 0,             # 0 = 不设下限, 合法
                                     "timeout_seconds": 180, "max_tokens_member": 3000.0}})


def test_validate_config_accepts_nonempty_seats():
    """seat 门只拒【空】值。非字符串 seat(如 seat: 1)放行,缺 seat 键放行(_seat_role 回落 '?')。
    收紧成"必须是 A-D"会是配置层的破坏性变更——v1.7.1 A1 新门误拒合法配置的教训,新门宁可窄。
    放行不等于"能跑通":角色解析侧的可跑性由 test_role_resolution_survives_nonstring_seat 独立锁住。"""
    moa.validate_config({"members": [{"name": "a", "seat": "A"},
                                     {"name": "b", "seat": 1},      # 非字符串: 放行
                                     {"name": "c"}],                # 无 seat 键: 放行
                         "options": {}})


@pytest.mark.parametrize("member", [
    {"name": "a", "seat": 1},              # YAML 数字 seat
    {"name": "a", "seat": 0},              # 0 是 falsy, 另走一条分支
    {"name": "a", "seat": False},          # bool
    {"name": "a", "seat": ["A"]},          # 不可哈希 → DEFAULT_SEAT_ROLE 的 (mode, seat) 元组键会崩
    {"name": "a", "seat": "A", "role": 123},   # 非字符串的显式 role: 同一崩点的另一入口
])
@pytest.mark.parametrize("mode", ["review", "decide", "brainstorm"])
def test_role_resolution_survives_nonstring_seat(member, mode):
    """校验门放行的配置必须真能跑完角色解析。

    `_seat_role` 把 seat 原样当角色键,`load_role_prompt` 再喂给 `re.escape` —— 非字符串
    就是裸 TypeError,正是 seat 门本该拦掉的那种报错,只是换了个值。校验放行却在下游崩,
    等于把 CHANGELOG/SKILL.md 里"seat: 1 仍可跑"的承诺写成假话(预审 H1)。
    可跑 = 落到通用兜底角色串,不是崩。"""
    moa.validate_config({"members": [member], "options": {}})
    txt = moa.load_role_prompt(mode, moa._seat_role(member, mode), {})
    assert isinstance(txt, str) and txt.strip()


@pytest.mark.parametrize("phase,fn", [
    ("discuss-turn", "cmd_discuss_turn"),
    ("discuss-prompt", "cmd_discuss_prompt"),
    ("discuss-blindvote", "cmd_discuss_blindvote"),
])
def test_discuss_requires_nonempty_seat_even_with_explicit_role(tmp_path, phase, fn):
    """generate/refine 放行"空 seat + 显式 role"(role 胜出,seat 不参与角色解析),但讨论里
    seat 还是发言者身份与 blindvote 文件名:写空会落到 blindvote_None.json,再被
    compute_discuss_stats 的 `if b.get("seat")` 当假值丢掉 —— 该席的盲投漂移静默消失,
    而漂移检测是讨论模式的三重反从众对冲之一(预审 M3)。故非空要求只在 discuss 入口生效。"""
    brief = tmp_path / "b.md"; brief.write_text("材料", encoding="utf-8")
    cfg = {"members": [{"name": "alpha", "seat": "A", "role": "feasibility_skeptic"},
                       {"name": "beta", "seat": None, "role": "security_auditor"}],
           "options": {"max_tokens_member": 100, "timeout_seconds": 60}}
    moa.validate_config(cfg)                     # 生成轮侧仍然合法
    args = types.SimpleNamespace(input=str(brief), member="alpha", collect_dir=str(tmp_path),
                                 mode="review", round=1, inject=None, blind=False, topic="")
    with pytest.raises(SystemExit) as ei:
        getattr(moa, fn)(args, cfg)
    assert "非空 seat" in str(ei.value) and "beta" in str(ei.value)


def test_validate_config_accepts_empty_seat_when_role_is_explicit():
    """显式写了 role 的席,seat 不参与角色解析(`_seat_role` 里 role 直接胜出),v1.7.1 上
    这种配置跑得好好的 —— 新门不得拒它(预审 M1)。decide 模式尤其常见:
    DEFAULT_SEAT_ROLE 按设计没有 decide 条目,角色全靠 member.role / custom_roles 注入。"""
    cfg = {"members": [{"name": "a", "seat": None, "role": "security_auditor",
                        "channel": "api", "model": "m"}], "options": {}}
    moa.validate_config(cfg)
    assert moa._seat_role(cfg["members"][0], "review") == "security_auditor"


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


# ---------- ISSUE-008(原 F5 告警升级为硬门): auto cli_kind + model 无 auggie_model → 拒绝启动 ----------

def test_validate_config_rejects_auto_cli_model_without_auggie_model():
    """channel=cli + auto(默认)+ 设了 model 但无 auggie_model → 拒绝启动。
    升级为硬门的理由不是「配置没生效」,而是该席会跑一个不可知的模型(auggie 只认 auggie_model,
    member.model 被静默顶替、model_used 记 None),让 synthesis.md 的家族构成披露硬规则不可执行。
    报错须同时给出两条修法,否则用户不知道该补 auggie_model 还是写 cli_kind。"""
    with pytest.raises(SystemExit) as e:
        moa.validate_config({"members": [{"name": "a", "channel": "cli", "model": "gpt5.6-sol"}],
                             "options": {}})
    msg = str(e.value)
    assert "auggie_model" in msg and "cli_kind" in msg


def test_validate_config_accepts_explicit_auggie_model_or_cli_kind():
    """显式 auggie_model 或显式 cli_kind → 模型可知,放行(出厂 config 全部走这条路径)。"""
    moa.validate_config({"members": [
        {"name": "a", "channel": "cli", "model": "x", "auggie_model": "gpt5.6-sol"},
        {"name": "b", "channel": "cli", "cli_kind": "codex", "model": "y"},   # 显式 kind 直接用 model
        {"name": "c", "channel": "cli", "cli_kind": "codex", "model": None},  # codex 默认模型: 合法
    ], "options": {}})


def test_validate_config_rejects_ambiguous_cli_in_fallback_links():
    """预审评审 #4: ISSUE-008 的硬门此前只看顶层 member,而 resolve_channel 会把每个 fallback
    项 merge 成 {**member, **fb} 再走同一套 auto→auggie 优先。于是 fallback 里的 cli 链同样会
    静默跑 auggie 的默认模型、model_used 记 None ——而 CHANGELOG 把规则写成"cli 席…被拒绝",
    用户会理所当然地以为 fallback 链也被覆盖了。"""
    with pytest.raises(SystemExit) as e:
        moa.validate_config({"members": [
            {"name": "a", "channel": "api", "model": "openai/gpt-5.6-sol",
             "fallback": [{"channel": "cli", "model": "gpt5.6-sol"}]}],
            "options": {}})
    msg = str(e.value)
    assert "fallback" in msg and ("auggie_model" in msg and "cli_kind" in msg)


def test_validate_config_allows_fallback_that_omits_channel():
    """预审评审 A1(阻断级回归): fallback 省略 channel 是合法写法——`resolve_channel` 按
    `fb.get("channel", "api")` 判,即默认 api。而 {**m, **fb} 会把【member 的】channel 继承进来,
    于是一个 cli 席挂的 api fallback 被误判成 cli 链并拒绝启动,报错还把它称作 channel=cli。
    这是启动即失败的误报,且恰好打在 ISSUE-008 想保护的那批用户身上。"""
    cfg = {"members": [{"name": "a", "channel": "cli",
                        "fallback": [{"model": "openai/gpt-5.6-sol", "protocol": "openrouter"}]}],
           "options": {}}
    moa.validate_config(cfg)                       # 必须放行
    # 且实跑确实把它展开成 api 链, 证明放行是对的而非放水
    kinds = [k for k, _, _ in moa.resolve_channel(cfg["members"][0])]
    assert "api" in kinds


def test_validate_config_still_rejects_fallback_with_explicit_cli_channel():
    """对照: fallback 显式写 channel=cli 且无 cli_kind/auggie_model —— 真歧义, 仍须拒。"""
    with pytest.raises(SystemExit):
        moa.validate_config({"members": [
            {"name": "a", "channel": "api", "model": "m",
             "fallback": [{"channel": "cli", "model": "gpt5.6-sol"}]}],
            "options": {}})


def test_validate_config_fallback_inherits_member_auggie_model():
    """member 上的 auggie_model 会被 merge 进 fallback({**member, **fb}),故不构成歧义——
    门必须按 merge 后的视图判,不能只看 fb 自己写了什么。"""
    moa.validate_config({"members": [
        {"name": "a", "channel": "api", "model": "x", "auggie_model": "gpt5.6-sol",
         "fallback": [{"channel": "cli", "model": "y"}]}],
        "options": {}})


def test_skipped_grace_record_has_usage_key():
    """预审评审 #7: CHANGELOG 承诺"失败席产物一律带 usage 键",而 _skipped_grace 自建 dict 时漏了。
    产物形状要一致,消费方才敢直接读 r["usage"]。"""
    r = moa._skipped_grace({"name": "a", "seat": "A", "model": "m"})
    assert "usage" in r and r["usage"] is None


def test_shipped_example_config_passes_validation():
    """出厂 config.example.yaml 必须通过全部校验门——ISSUE-008 的硬门若打到出厂配置就是回归。"""
    cfg = yaml.safe_load((Path(moa.SKILL_ROOT) / "assets" / "config.example.yaml")
                         .read_text(encoding="utf-8"))
    moa.validate_config(cfg)


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


@pytest.mark.parametrize("member,expect", [
    # codex 席 model 必须置空 —— config.example.yaml 的注释就是这么教的("codex 兜底: model 必须置空")。
    # YAML 显式 null 与「键不存在」在 dict.get(k, dflt) 里是两回事: 前者返回 None,
    # 进 f-string 的 :<28 宽度格式即 TypeError,dry-run 在文档教的写法上直接 traceback。
    ({"name": "a", "seat": "A", "channel": "cli", "cli_kind": "codex", "model": None}, "a    "),
    ({"name": "a", "seat": None, "channel": "api", "model": "m"}, "?"),        # seat 写空 → 占位 ?
    # protocol 是最后一列、无 :<宽度> 格式符, 故 None 不会抛 —— 但旧代码会把字面 "None" 印给用户。
    # 断言渲染成占位 "-", 否则这条参数化等于什么都没测(预审 INFO)。
    ({"name": "a", "seat": "A", "channel": "api", "model": "m", "protocol": None}, "m  "),
])
def test_dry_run_renders_explicit_null_fields(member, expect, capsys):
    """dry-run 是 SKILL.md 第 2 步给用户过目的那张表: 任何字段显式为 null 都不得崩,
    且要渲染成占位符而不是字面 "None"。"""
    moa.dry_run({"members": [member], "options": {}}, "review", "material", "", 0)
    out = capsys.readouterr().out
    assert "DRY RUN" in out and expect in out
    assert "None" not in out          # 任何一列都不得把 None 原样印给用户


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
    def fake_repair(cfg, system, user, temp, max_tokens, timeout, schema=None, **_):
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


def test_stats_token_usage_counts_successful_seats_only():
    """`total_tokens` 的口径始终是【换回了意见的成本】—— README 的成本倍数按它读,
    ISSUE-012 重新引入 wasted_* 时也不得改写它(只加字段,不动既有字段语义)。

    v1.7.0 曾加 wasted_* 又撤回,因为它两个方向同时错(截断重试在 call_model 循环里就丢了
    usage → 21000 报成 0;provider 省略 usage 时 _merge_usage({}) 全零却为真 → 没花钱的席
    被计成 wasted_members=1)。ISSUE-012 用逐席账本修掉根因后才重新引入,见
    test_stats_reports_wasted_spend_without_touching_existing_totals。"""
    ok = _res("a", "A", {"verdict": "pass", "confidence": 0.5, "issues": []})
    ok["usage"] = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    ok["usage_total"] = {"total_tokens": 140, "calls": 1}
    burned = _res("b", "B", None, err_class="parse")      # 已计费却没产出
    burned["usage"] = {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80}
    burned["usage_total"] = {"total_tokens": 80, "calls": 1}
    tu = moa.compute_stats("review", [ok, burned])["token_usage"]
    assert tu["total_tokens"] == 140          # 只算成功席, 不因 wasted_* 的引入而变
    assert tu["billed_members"] == 1
    assert tu["wasted_tokens"] == 80          # 失败席的 80 单列, 不并入上面的 140
    assert tu["wasted_members"] == 1


def test_stats_separates_skipped_from_real_failures():
    """ISSUE-011: 宽限窗主动放弃的席与真故障席语义不同。members_failed 保持「全部非成功席」
    (既有读数不变),另列 members_skipped 作子集,仲裁人可算真失败 = failed - skipped。"""
    ok = _res("a", "A", {"verdict": "pass", "confidence": 0.5, "issues": []})
    broke = _res("b", "B", None, err_class="server")
    dropped = _res("c", "C", None, err_class="skipped_grace")
    s = moa.compute_stats("review", [ok, broke, dropped])
    assert s["members_ok"] == 1
    assert s["members_failed"] == 2           # 含被放弃席,与旧版一致
    assert s["members_skipped"] == 1          # 其中 1 席是主动放弃,不是故障
    assert s["degraded"] is True


def test_stats_roster_flags_unknown_model():
    """ISSUE-008 配套: model_used=None(如出厂 fallback 的 cli_kind:codex + model:null)
    意味着该席跑的是通道默认模型、家族不可知。roster 显式标 model_known=False,
    让 synthesis.md 的家族构成披露能把这几席排除在计数外,而不是把 null 当缺数据忽略。"""
    known = _res("a", "A", {"verdict": "pass", "confidence": 0.5, "issues": []})
    unknown = _res("b", "B", {"verdict": "pass", "confidence": 0.5, "issues": []})
    unknown["model_used"] = None              # codex 默认模型
    roster = {r["name"]: r for r in moa.compute_stats("review", [known, unknown])["roster"]}
    assert roster["a"]["model_known"] is True
    assert roster["b"]["model_known"] is False


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


def _quorum_race_probe(straggler_result):
    """构造「窗到期检查执行时,落伍席其实已经跑完」的确定性竞态。

    `dispatch_with_quorum` 在同一次循环里先收割 done、再登记宽限窗、再查到期。落伍席若在
    【收割回调执行期间】跑完,它仍留在 pending 集合里,于是到期检查把一个已完成的 future
    当成"还在跑"处理掉。用 Event 把落伍席钉在 quorum 达成那一刻放行,窗设 0 让到期必然发生。"""
    gate = threading.Event()

    def fn(m):
        if m["name"] == "c":
            gate.wait(5)
            return straggler_result
        return {"name": m["name"], "seat": m["seat"], "role": "r", "model_used": "m",
                "channel_used": "api", "parsed": {"verdict": "pass", "issues": []},
                "usage": {"total_tokens": 30}, "latency_s": 0.0, "error": None, "err_class": None}

    def on_done(r):
        if r["name"] == "b":            # quorum 达成的那一刻放行 c,并等它真正跑完
            gate.set()
            time.sleep(0.25)

    members = [{"name": n, "seat": n.upper()} for n in ("a", "b", "c")]
    res = moa.dispatch_with_quorum(members, fn, quorum_target=2, grace_s=0, on_done=on_done)
    return {r["name"]: r for r in res}


def test_finished_straggler_keeps_its_real_failure_not_skipped_grace():
    """落伍席在窗到期的同一瞬间其实已跑完 → 必须用它的【真结果】,不得记成 skipped_grace。

    记错的代价是双向的: SKILL.md 教仲裁人按「真故障席 = members_failed - members_skipped」
    读数,把一个真 401 洗成"只是慢"会让这个差算出 0 个故障;可操作的报错提示(check API key)
    与已计费的 usage 也一并丢掉——正是 v1.7.0 想堵的漏账口。"""
    real = {"name": "c", "seat": "C", "role": "r", "model_used": "m3", "channel_used": "api",
            "parsed": None, "usage": {"total_tokens": 900}, "latency_s": 0.1,
            "error": "HTTP 401 auth [auth] check API key / credits", "err_class": "auth"}
    by = _quorum_race_probe(real)
    assert by["c"]["err_class"] == "auth"              # 不是 skipped_grace
    assert "401" in by["c"]["error"]
    assert by["c"]["usage"] == {"total_tokens": 900}   # 已计费的账没丢
    st = moa.compute_stats("review", list(by.values()))
    assert st["members_failed"] - st["members_skipped"] == 1   # 真故障席算得出来


def test_finished_straggler_keeps_its_successful_opinion():
    """同一竞态的成功席变体: 丢掉的是一份【已付费】的委员意见。本例里 c 还是唯一投 fail
    并给出 blocker 的一席——丢了它,stats 报出全票 pass、零 blocker 的假共识,
    而"识破假共识"正是这个委员会存在的理由。"""
    real = {"name": "c", "seat": "C", "role": "r", "model_used": "m3", "channel_used": "api",
            "parsed": {"verdict": "fail", "confidence": 0.9,
                       "issues": [{"title": "致命问题", "severity": "blocker"}]},
            "usage": {"total_tokens": 1200}, "latency_s": 0.1, "error": None, "err_class": None}
    by = _quorum_race_probe(real)
    assert by["c"]["err_class"] is None and by["c"]["parsed"]["verdict"] == "fail"
    st = moa.compute_stats("review", list(by.values()))
    assert st["members_ok"] == 3
    assert st["verdict_tally"] == {"pass": 2, "fail": 1}       # 分歧保住了
    assert st["issue_count_by_severity"]["blocker"] == 1
    assert st["token_usage"]["total_tokens"] == 1260           # 30+30+1200, 账齐


def test_straggler_worker_exception_fails_only_that_seat():
    """落伍席的 worker 自己抛异常时,`fut.result()` 会把它原样重抛,整轮 dispatch 随之炸掉,
    且 `abandoned` 仍为 False → `shutdown(wait=True)` 还要 join 全部线程(ISSUE-009 那种挂住)。
    该席记成失败即可,不该拖垮其余已付费的席(预审 M2)。

    可达性:`_dispatch_channels` 内部 catch 了 Exception,但 worker 在它之前还跑
    `resolve_channel(member)` 与 `opts["timeout_seconds"]` —— 而 `options: {}` 是
    validate_config 放行的配置。"""
    boom = RuntimeError("worker blew up")

    class _Raiser(dict):
        pass

    gate = threading.Event()

    def fn(m):
        if m["name"] == "c":
            gate.wait(5)
            raise boom
        return {"name": m["name"], "seat": m["seat"], "role": "r", "model_used": "m",
                "channel_used": "api", "parsed": {"verdict": "pass", "issues": []},
                "usage": {"total_tokens": 30}, "latency_s": 0.0, "error": None, "err_class": None}

    def on_done(r):
        if r["name"] == "b":
            gate.set()
            time.sleep(0.25)

    members = [{"name": n, "seat": n.upper()} for n in ("a", "b", "c")]
    res = moa.dispatch_with_quorum(members, fn, quorum_target=2, grace_s=0, on_done=on_done)
    by = {r["name"]: r for r in res}
    assert set(by) == {"a", "b", "c"}                 # 其余两席的结果没被连累
    assert by["c"]["parsed"] is None
    assert "worker blew up" in by["c"]["error"]


# ---------- ISSUE-009: 弃席后进程快速退出(不等 atexit join 落伍线程)----------

def test_abandoning_straggler_sets_fast_exit_flag(monkeypatch):
    """弃席时置位模块标志,main() 据此跳过解释器退出阶段的线程 join。
    实测旧行为: dispatch 已在 0.55s 返回,进程要到 6.1s 才退出(默认 timeout 下最坏 4 分钟)。"""
    monkeypatch.setattr(moa, "_ABANDONED_STRAGGLERS", False)
    members = [{"name": "fast1", "seat": "A"}, {"name": "fast2", "seat": "B"},
               {"name": "slow", "seat": "C"}]

    def fn(m):
        if m["name"] == "slow":
            time.sleep(0.3)
        return {"name": m["name"], "seat": m["seat"], "parsed": {"ok": 1}, "role": "r",
                "channel_used": "api", "latency_s": 0.0, "model_used": "m",
                "err_class": None, "error": None}

    res = moa.dispatch_with_quorum(members, fn, quorum_target=2, grace_s=0.02)
    assert any(r["err_class"] == "skipped_grace" for r in res)
    assert moa._ABANDONED_STRAGGLERS is True


def test_fast_exit_never_fires_outside_the_cli_entrypoint(monkeypatch):
    """预审评审 #5: `_ABANDONED_STRAGGLERS` 是永不复位的模块全局,已有两个 grace 测试会把它
    留成 True 直到会话结束。今天只是潜伏——三个 main() 测试走的路径都在快速退出前返回或抛
    SystemExit;但只要有人给 dry-run / discuss-prompt 这类【正常返回】的路径补一个 main() 测试,
    os._exit(0) 就会在 pytest 进程里开火,**以退出码 0 杀掉测试进程**,CI 于是在跳过了剩余全部
    用例的情况下报绿。所以快速退出必须再要求"确实是 CLI 入口",单凭 abandoned 标志不够。"""
    killed = []
    monkeypatch.setattr(moa.os, "_exit", lambda code: killed.append(code))
    monkeypatch.setattr(moa, "_ABANDONED_STRAGGLERS", True)
    monkeypatch.setattr(moa, "_RUNNING_AS_CLI", False)   # 库调用 / 测试直接调 main()
    moa._fast_exit_if_stragglers()
    assert killed == []                                  # 绝不在测试进程里开火
    monkeypatch.setattr(moa, "_RUNNING_AS_CLI", True)    # 真正的 `python moa.py …`
    moa._fast_exit_if_stragglers()
    assert killed == [0]


def test_fast_exit_defers_while_a_cli_call_is_in_flight(monkeypatch, tmp_path):
    """预审评审 #3 + fix-3 尾: os._exit 杀掉的不只是 TemporaryDirectory 的清理 finalizer,
    还有 subprocess.run 的超时看门狗。被弃的 auggie/codex 子进程会被 reparent 后无界地跑下去
    (README 记录过 auggie 内部重试 >7 分钟,按上游价 +40% 计费),同时它的 prompt.txt(整份简报)
    留在系统临时目录。所以只在【没有 CLI 调用在飞】时快速退出;有就退回常规退出——那正是
    v1.7.0 之前的行为,不构成回归,只是放弃这一种情况下的提速。"""
    killed = []
    monkeypatch.setattr(moa.os, "_exit", lambda code: killed.append(code))
    monkeypatch.setattr(moa, "_RUNNING_AS_CLI", True)
    monkeypatch.setattr(moa, "_ABANDONED_STRAGGLERS", True)
    monkeypatch.setattr(moa, "_ACTIVE_CLI_TMPDIRS", {str(tmp_path)})
    moa._fast_exit_if_stragglers()
    assert killed == []                       # 有 CLI 在飞: 不强杀, 让看门狗与清理器跑完
    monkeypatch.setattr(moa, "_ACTIVE_CLI_TMPDIRS", set())
    moa._fast_exit_if_stragglers()
    assert killed == [0]                      # 无 CLI 在飞: 快速退出照旧


def test_auggie_retry_timeout_arg_is_integral(monkeypatch):
    """预审评审 #8: ISSUE-007 之后 cli 修复轮拿到的是 _remaining() 的【浮点】剩余预算,
    而 --retry-timeout 由 str(max(30, timeout // 3)) 拼出 → '73.0'。auggie 的参数解析若严格
    要整数,修复轮就在出厂委员会的 A/C/D 三席上静默失效(非零退出记 err_class=cli)。"""
    seen = {}

    class _P:
        returncode = 0
        stdout = b'{"result": "{\\"v\\": 1}"}'
        stderr = b""

    monkeypatch.setattr(moa, "_which", lambda e: "/usr/bin/auggie")
    monkeypatch.setattr(moa.subprocess, "run", lambda cmd, **kw: (seen.setdefault("cmd", cmd), _P())[1])
    moa.call_cli_auggie({"model": "m"}, "s", "u", 219.7)      # 浮点预算,如 _remaining 所返
    arg = seen["cmd"][seen["cmd"].index("--retry-timeout") + 1]
    assert "." not in arg, f"--retry-timeout 收到非整数: {arg!r}"


def test_fast_exit_is_noop_without_abandoned_stragglers(monkeypatch):
    """没弃过席就必须原样返回——否则每一次干净运行都会被 os._exit 强杀,
    连 sys.exit 的非零码都传不出去。"""
    killed = []
    monkeypatch.setattr(moa.os, "_exit", lambda code: killed.append(code))
    monkeypatch.setattr(moa, "_RUNNING_AS_CLI", True)
    monkeypatch.setattr(moa, "_ABANDONED_STRAGGLERS", False)
    moa._fast_exit_if_stragglers()
    assert killed == []                       # 干净路径: 不碰进程
    monkeypatch.setattr(moa, "_ABANDONED_STRAGGLERS", True)
    moa._fast_exit_if_stragglers()
    assert killed == [0]                      # 弃过席: 以 0 退出(干净路径才会走到这里)


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


# ---------- --round 取值门: 轮次是编排计数器, 0/负数不是合法轮次 ----------

@pytest.mark.parametrize("phase,bad", [
    ("refine", "0"),          # 0 就是生成轮本身, 不存在"精炼到第 0 轮"
    ("refine", "-1"),
    ("discuss-turn", "0"),    # 旧行为: 静默写进 transcript, 后发言者读到「第 0 轮」
    ("discuss-turn", "-1"),
    ("discuss-prompt", "0"),
    ("stats", "-1"),          # stats 允许 0(读生成轮产物), 但不允许负数
    ("refine", "abc"),        # 非整数
])
def test_main_rejects_out_of_range_round(tmp_path, monkeypatch, phase, bad):
    brief = tmp_path / "b.md"; brief.write_text("x", encoding="utf-8")
    argv = ["moa.py", phase, "--collect-dir", str(tmp_path), "--round", bad]
    if phase != "stats":
        argv += ["--input", str(brief)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as ei:
        moa.main()
    assert ei.value.code == 2          # argparse 的用法错误退出码


@pytest.mark.parametrize("phase,good", [("refine", "1"), ("discuss-turn", "1"),
                                        ("stats", "0"), ("stats", "2")])
def test_main_accepts_valid_round(tmp_path, monkeypatch, phase, good):
    """防误拒: 合法轮次必须照常放行(门只拦 0/负数/非整数)。"""
    brief = tmp_path / "b.md"; brief.write_text("x", encoding="utf-8")
    argv = ["moa.py", phase, "--collect-dir", str(tmp_path), "--round", good]
    if phase != "stats":
        argv += ["--input", str(brief)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as ei:
        moa.main()                     # 后续必然因缺产物/缺 config 退出, 但不是 argparse 的 code 2
    assert ei.value.code != 2


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


# ---------- ISSUE-002 补全: 精炼统计的标量字段同样要类型守卫 ----------
# compute_stats / compute_discuss_stats 当初都补了守卫(见上方 malformed 用例),
# compute_refine_stats 被漏掉: verdict / revised_claimed_option 在这里既当 dict 键
# 又当 set 元素,模型把它写成 list/dict 就整轮聚合崩栈 —— 其余席已付费的产物一并作废。

@pytest.mark.parametrize("prior_v,cur_v", [
    (["pass"], "pass"),          # 上一轮畸形 → _majority_verdict 的 tally[v]
    ("pass", ["pass"]),          # 本轮畸形 → cur_verdicts 集合
    ({"v": "pass"}, "pass"),     # dict 同样不可哈希
])
def test_refine_stats_review_unhashable_verdict_no_crash(prior_v, cur_v):
    prior = [_rf("a", prior_v, []), _rf("b", "pass", [])]
    refine = [_rf("a", cur_v, []), _rf("b", "pass", [])]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["round_members_ok"] == 2          # 两席 parsed 都是对象 → 仍算成功席
    assert isinstance(s["stance_tally"], dict)


def test_refine_stats_no_early_stop_when_verdict_unreadable():
    """不可读的 verdict 不等于"和别人一致": 有席立场读不出来时不得建议早停
    (否则仲裁人会据此少跑一轮,而那一轮正是要补上这席立场的)。"""
    prior = [_rf("a", "pass", []), _rf("b", "pass", [])]
    refine = [_rf("a", "pass", []), _rf("b", ["pass"], [])]
    s = moa.compute_refine_stats("review", prior, refine)
    assert s["early_stop_suggested"] is False


def test_refine_stats_decide_unhashable_option_no_crash():
    prior = [{"name": "a", "seat": "A", "parsed": {"claimed_option": "PG"}},
             {"name": "b", "seat": "C", "parsed": {"claimed_option": "Mongo"}}]
    refine = [{"name": "a", "seat": "A",
               "parsed": {"revised_claimed_option": {"opt": "PG"}, "cross_exam": []}},
              {"name": "b", "seat": "C",
               "parsed": {"revised_claimed_option": "PG", "cross_exam": []}}]
    s = moa.compute_refine_stats("decide", prior, refine)
    assert s["round_members_ok"] == 2
    assert s["early_stop_suggested"] is False   # 一席认领选项不可读 → 不构成全一致


# ---------- 产物读写 round-trip + 精炼产物排除 ----------

def test_member_write_load_roundtrip(tmp_path):
    r = _res("a", "A", {"verdict": "pass", "confidence": 0.5, "issues": []})
    moa.write_member(tmp_path, r)
    moa.write_member(tmp_path, {**r, "name": "b"}, round_no=1)  # 精炼产物
    gen = moa.load_members(tmp_path, round_no=0)
    assert [m["name"] for m in gen] == ["a"]  # round_no=0 排除 .r1
    r1 = moa.load_members(tmp_path, round_no=1)
    assert [m["name"] for m in r1] == ["b"]


# ---------- ISSUE-005 的漏网门: --models 在 validate_config 之前就碰未校验的 cfg ----------

def test_main_names_the_error_when_config_is_not_a_mapping(tmp_path, monkeypatch):
    """main() 的次序是 resolve_config → apply_custom_committee → validate_config。
    validate_config 早就为「顶层不是映射」写好了具名报错, 但 --models 让 apply_custom_committee
    先对未校验的 cfg 做 dict(cfg):空 YAML → None → 裸 TypeError traceback, 绕过了那道门。
    (不给 --models 时同一份 config 报的是具名错误 —— 见下一条, 两条路径此前不对称。)"""
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("", encoding="utf-8")
    brief = tmp_path / "b.md"
    brief.write_text("材料", encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["moa.py", "dry-run", "--config", str(cfg), "--input", str(brief),
                         "--models", "openai/gpt-4o"])
    with pytest.raises(SystemExit) as ei:
        moa.main()
    # 断言【具体那一句】: 只查 "[config]" 的话, 守卫改成 `return {}`(报错变成"缺 members 列表")
    # 照样绿 —— 而本条修复的立意恰恰是"两条路径给同一句"(预审 L3)。
    assert "顶层必须是 YAML 映射" in str(ei.value)


def test_main_names_the_same_error_without_models(tmp_path, monkeypatch):
    """对照组: 同一份空 config 不给 --models 时走的是 validate_config 的具名门。
    两条路径必须给同一类报错, 否则「具名报错而非 traceback」只对一半用户成立。"""
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("", encoding="utf-8")
    brief = tmp_path / "b.md"
    brief.write_text("材料", encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["moa.py", "dry-run", "--config", str(cfg), "--input", str(brief)])
    with pytest.raises(SystemExit) as ei:
        moa.main()
    assert "顶层必须是 YAML 映射" in str(ei.value)


def test_models_still_overrides_members_on_a_valid_config():
    """防误拒: 新门只拦非映射, 合法 config + --models 仍然整体替换 members, options 原样保留。"""
    cfg = {"members": [{"name": "old", "channel": "api", "model": "m"}],
           "options": {"max_tokens_member": 100}}
    out = moa.apply_custom_committee(cfg, types.SimpleNamespace(models="a/m1,b/m2", members=None))
    assert [m["model"] for m in out["members"]] == ["a/m1", "b/m2"]
    assert out["options"] == {"max_tokens_member": 100}


# ---------- 已发布制品的可发现性信号: 未设的 options 键代入了默认值 ----------

def _partial_opts_config(tmp_path, options_body):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("members:\n  - name: a\n    seat: A\n    channel: api\n    model: m\n"
                   + options_body, encoding="utf-8")
    brief = tmp_path / "b.md"
    brief.write_text("材料", encoding="utf-8")
    return cfg, brief


def test_main_announces_option_defaults_once(tmp_path, monkeypatch, capsys):
    """B1(预审):本版把「半块 options = 拒绝启动、零支出」改成「按用户没写过的值开跑并计费」,
    属已发布制品的用户可见默认行为变更,runbook 要求一次性可发现信号(先例 _warn_budget_semantics_once)。
    信号打在 main() 里、每次运行一行,而不是打在 _opt() 里——后者每席每链都会被调、还跑在 worker
    线程里,会交错刷屏。"""
    cfg, brief = _partial_opts_config(tmp_path, "options:\n  min_successful_members: 1\n")
    monkeypatch.setattr(sys, "argv",
                        ["moa.py", "dry-run", "--config", str(cfg), "--input", str(brief),
                         "--no-model-check"])
    moa.main()
    err = capsys.readouterr().err
    assert err.count("[options]") == 1                    # 一行, 不是每键一行/每席一行
    for key in ("timeout_seconds", "max_tokens_member", "grace_seconds"):
        assert key in err                                 # 点名哪几个键没设
    assert "3000" in err and "180" in err                 # 代入了什么值
    line = [ln for ln in err.splitlines() if "[options]" in ln][0]
    assert "min_successful_members" not in line           # 用户【写过】的键不得被列成"未设"
    assert "assets/config.example.yaml" in err            # 怎么固定住
    assert "v1.9.0" in err                                # 本来会崩 —— 用户真正需要的那半句
    assert "pin v1.9.0" in err                            # 回退路径


def test_main_stays_silent_when_options_are_complete(tmp_path, monkeypatch, capsys):
    """防误拒/防刷屏: 出厂示例 config 四个键齐全 ⇒ 默认用户永远看不到这行。
    信号的受众必须精确等于行为变更的受众(过去会崩的那批 config)。"""
    shipped = yaml.safe_load(
        (Path(moa.__file__).resolve().parent.parent / "assets" / "config.example.yaml")
        .read_text(encoding="utf-8"))
    body = "options:\n" + "".join(
        f"  {k}: {v}\n" for k, v in shipped["options"].items())
    cfg, brief = _partial_opts_config(tmp_path, body)
    monkeypatch.setattr(sys, "argv",
                        ["moa.py", "dry-run", "--config", str(cfg), "--input", str(brief),
                         "--no-model-check"])
    moa.main()
    assert "[options]" not in capsys.readouterr().err


def test_default_options_track_the_shipped_example():
    """M1(预审):CHANGELOG 与代码注释都把「DEFAULT_OPTIONS 取自出厂示例(grace 除外)」当不变量
    在讲,却没有任何机械门——把 max_tokens_member 默认值改成 8000 全量仍绿,因为两条 dispatch
    用例写的是 `assert seen[...] == moa.DEFAULT_OPTIONS[...]`(自指断言,按定义抓不住值的改变)。
    本门同时守反向漂移:示例被改而默认没跟。"""
    shipped = yaml.safe_load(
        (Path(moa.__file__).resolve().parent.parent / "assets" / "config.example.yaml")
        .read_text(encoding="utf-8"))["options"]
    assert set(moa.DEFAULT_OPTIONS) == set(shipped)          # 键集同集, 防一侧新增键
    for key, value in (("timeout_seconds", 180), ("max_tokens_member", 3000),
                       ("min_successful_members", 2)):
        assert moa.DEFAULT_OPTIONS[key] == shipped[key] == value
    # grace_seconds 是唯一的显式例外: 示例的 90 是给重推理旗舰席的建议值, 脚本 fallback 保持 30
    # 向后兼容(v1.6.0 起的既有约定)。两个数都钉住, 任何一侧被"对齐"都会红。
    assert shipped["grace_seconds"] == 90
    assert moa.DEFAULT_OPTIONS["grace_seconds"] == 30


# ---------- model 预检(tasks/specs/model-preflight.md): dry-run 先查再用 ----------

@pytest.mark.parametrize("member,expect", [
    # 能比对的: openrouter 协议(默认)+ 未自定义 base_url
    ({"name": "a", "seat": "A", "channel": "api", "model": "live/one"}, ["ok"]),
    ({"name": "a", "seat": "A", "channel": "api", "model": "retired/x"}, ["unknown"]),
    # 不能比对的一律 skipped, 不得产生假警报
    ({"name": "a", "seat": "A", "channel": "cli", "cli_kind": "codex"}, ["skipped"]),
    ({"name": "a", "seat": "A", "channel": "subagent"}, ["skipped"]),
    ({"name": "a", "seat": "A", "channel": "api", "model": "m",
      "base_url": "http://localhost:8000/v1"}, ["skipped"]),
    ({"name": "a", "seat": "A", "channel": "api", "model": "m",
      "protocol": "openai"}, ["skipped"]),
    # 缺 model: 与 call_model 的 startup 报错同源, 预检要提前说
    ({"name": "a", "seat": "A", "channel": "api"}, ["missing"]),
    # fallback 展开: 口径必须与 resolve_channel 的 {**member, **fb} 继承一致
    ({"name": "a", "seat": "A", "channel": "api", "model": "live/one",
      "fallback": [{"model": "retired/x"}]}, ["ok", "unknown"]),
    ({"name": "a", "seat": "A", "channel": "api", "model": "live/one",
      "fallback": [{"protocol": "openai", "model": "retired/x"}]}, ["ok", "skipped"]),
])
def test_check_model_slugs_classifies_each_link(member, expect):
    live = {"live/one", "live/two"}
    got = [st for _label, st, _detail in moa.check_model_slugs({"members": [member]}, live)]
    assert got == expect


def test_fetch_openrouter_models_is_fail_soft(monkeypatch):
    """预检是可发现性工具, 不是门禁: 离线/端点变更/超时一律回 None, 由调用方降级成一行说明。"""
    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(moa, "_opener_for", boom)
    assert moa.fetch_openrouter_models(timeout=1) is None


def test_dry_run_reports_slug_status(tmp_path, monkeypatch, capsys):
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "live/one"},
                       {"name": "b", "seat": "B", "channel": "api", "model": "retired/x"}],
           "options": {}}
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: {"live/one"})
    moa.dry_run(cfg, "review", "材料", None, 1, model_check=True)
    out = capsys.readouterr().out
    assert "live/one" in out and "retired/x" in out
    assert "unknown" in out or "不在" in out


def test_dry_run_model_check_degrades_without_network(monkeypatch, capsys):
    """取不到列表时只多一行说明, 不改变任何既有输出行, 也不抛。"""
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "live/one"}],
           "options": {}}
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: None)
    moa.dry_run(cfg, "review", "材料", None, 1, model_check=True)
    out = capsys.readouterr().out
    assert "确认无误后去掉 dry-run 正式运行。" in out          # 既有收尾行仍在
    assert "跳过" in out or "skip" in out.lower()


def test_dry_run_model_check_off_makes_no_network_call(monkeypatch, capsys):
    """--no-model-check: 一次网络调用都不许发(离线/气隙环境的出路)。"""
    def boom(**k):
        raise AssertionError("关闭预检后不得取模型列表")
    monkeypatch.setattr(moa, "fetch_openrouter_models", boom)
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "m"}], "options": {}}
    moa.dry_run(cfg, "review", "材料", None, 1, model_check=False)
    assert "确认无误后去掉 dry-run 正式运行。" in capsys.readouterr().out


def test_shipped_example_api_slugs_are_all_checkable_and_named():
    """出厂示例的每条 api 链都要能被预检覆盖(不是 skipped), 否则这个功能对默认用户等于不存在。"""
    shipped = yaml.safe_load(
        (Path(moa.__file__).resolve().parent.parent / "assets" / "config.example.yaml")
        .read_text(encoding="utf-8"))
    rows = moa.check_model_slugs(shipped, {"openai/gpt-5.6-sol", "google/gemini-3.1-pro-preview"})
    api_rows = [r for r in rows if r[1] != "skipped"]
    assert api_rows, "出厂示例应当至少有一条可比对的 api 链"
    assert all(st == "ok" for _l, st, _d in api_rows), [r for r in api_rows if r[1] != "ok"]


@pytest.mark.parametrize("member,expect", [
    # H1(预审): fallback 省略 channel = api 链(resolve_channel:712 用 fb.get("channel","api")),
    # 按 {**member, **fb} 的继承值去判会把它错判成 cli/subagent 而静默漏检。
    # test_validate_config_allows_fallback_that_omits_channel 已把这条语义钉成契约。
    ({"name": "b", "seat": "B", "channel": "cli", "cli_kind": "auggie", "model": "gpt5.6-sol",
      "fallback": [{"protocol": "openrouter"}]}, ["skipped", "unknown"]),
    ({"name": "b", "seat": "B", "channel": "cli", "cli_kind": "auggie",
      "fallback": [{"model": "retired/x"}]}, ["skipped", "unknown"]),
    ({"name": "b", "seat": "B", "channel": "subagent",
      "fallback": [{"model": "live/one"}]}, ["skipped", "ok"]),
    # 反方向不得误报: fallback 显式写 cli/subagent 时确实不比对
    ({"name": "b", "seat": "B", "channel": "api", "model": "live/one",
      "fallback": [{"channel": "cli", "cli_kind": "codex"}]}, ["ok", "skipped"]),
])
def test_check_model_slugs_uses_resolve_channel_semantics(member, expect):
    live = {"live/one", "live/two"}
    got = [st for _l, st, _d in moa.check_model_slugs({"members": [member]}, live)]
    assert got == expect


def test_dry_run_defaults_to_no_network():
    """H3(预审): 「测试全离线」此前只写在 docstring 里, 没有用例钉住。默认值一旦被"统一"成 True,
    套件在无网 CI 上照样全绿(fail-soft), 在有网 CI 上静默发出真实请求。"""
    import inspect
    assert inspect.signature(moa.dry_run).parameters["model_check"].default is False


def test_cli_no_model_check_flag_actually_disables_the_fetch(tmp_path, monkeypatch):
    """H3: --no-model-check 的接线此前零覆盖(变异里"恒开"和"恒关"都能全绿通过)。"""
    cfg, brief = _partial_opts_config(tmp_path, "options: {}\n")
    calls = []
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: calls.append(1) or {"m"})
    base = ["moa.py", "dry-run", "--config", str(cfg), "--input", str(brief)]
    monkeypatch.setattr(sys, "argv", base + ["--no-model-check"])
    moa.main()
    assert calls == [], "带 --no-model-check 时不得取模型列表"
    monkeypatch.setattr(sys, "argv", base)
    moa.main()
    assert calls == [1], "不带该 flag 时应当恰好取一次"


def test_fetch_openrouter_models_treats_empty_list_as_unavailable(monkeypatch):
    """M2(预审): 端点 200 但列表为空时, 必须整体降级为"跳过", 不能把每条链刷成 UNKNOWN ——
    那会让用户去改一份本来正确的 config。"""
    class _Resp:
        def read(self): return b'{"data": []}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(moa, "_opener_for", lambda url: types.SimpleNamespace(
        open=lambda req, timeout=None: _Resp()))
    assert moa.fetch_openrouter_models(timeout=1) is None


def test_model_list_request_carries_no_credentials(monkeypatch):
    """M3(预审): 「不带 Authorization」是三处加粗承诺, 但 leak-check 只做静态字面扫描,
    抓不到"把环境变量塞进请求头"。这里是它唯一的机械门。"""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-must-not-be-sent")
    seen = {}

    class _Resp:
        def read(self): return b'{"data": [{"id": "x/y"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_opener(url):
        def _open(req, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["method"], seen["data"], seen["url"] = req.get_method(), req.data, req.full_url
            return _Resp()
        return types.SimpleNamespace(open=_open)

    monkeypatch.setattr(moa, "_opener_for", fake_opener)
    assert moa.fetch_openrouter_models(timeout=1) == {"x/y"}
    assert "authorization" not in seen["headers"]
    assert not any("sk-must-not-be-sent" in str(v) for v in seen["headers"].values())
    assert seen["method"] == "GET" and seen["data"] is None


@pytest.mark.parametrize("model,expect_status", [
    ("live/one", "ok"),
    # 路由后缀不做"剥掉再比基座 id"的回落(delta D-M1 证伪了上一轮的 L1 修法:它兜不到任何真实
    # 形态, 却把 `…:typo` 和光秃秃的尾随冒号洗成 ok)。带冒号一律 unknown, 文案里给基座 id 提示。
    ("live/one:nitro", "unknown"),
    ("live/one:free", "ok"),         # :free 是真实 id 形态, 走精确匹配
    ("gone/x:nitro", "unknown"),
    (None, "missing"),               # L2: 显式 model: null 与"没写"同判
    ("", "missing"),
    # delta-2 L3: 非字符串必须判 unknown 而非 missing —— 判据是运行时真实路径:
    # `model: 123` 过得了 call_model 的 `not cfg.get("model")` 前置检查, 一路进 payload,
    # provider 返 400(err_class=client), 不是 err_class=startup, 所以不属于"没写 model"那一格。
    (123, "unknown"),
    (True, "unknown"),
    (["a", "b"], "unknown"),
])
def test_slug_variants_and_explicit_null(model, expect_status):
    live = {"live/one", "live/one:free"}
    member = {"name": "a", "seat": "A", "channel": "api", "model": model}
    got = moa.check_model_slugs({"members": [member]}, live)
    assert got[0][1] == expect_status


def test_model_check_summary_line_counts_actionable_links(monkeypatch, capsys):
    """L5(预审): 那句「↑ N 条链现在就能看出会失败」删掉后套件全绿 —— 它是用户唯一被告知
    "该动手了"的地方, 补一条钉住。"""
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "gone/x"},
                       {"name": "b", "seat": "B", "channel": "api"},
                       {"name": "c", "seat": "C", "channel": "api", "model": "live/one"}],
           "options": {}}
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: {"live/one"})
    moa.dry_run(cfg, "review", "材料", None, 1, model_check=True)
    out = capsys.readouterr().out
    assert "2 条链" in out                      # 一条 unknown + 一条 missing, ok 的不计


def test_model_list_timeout_stays_short():
    """取列表的超时是写进 CHANGELOG / 两份 README / SKILL.md 的取舍值(黑洞网络下 dry-run 会走满它,
    而 dry-run 是当着用户面跑的)。与 grace 30-vs-90 同类:文案承诺的数字要有机械门(预审 M1)。"""
    assert moa._MODEL_LIST_TIMEOUT == 5
    # 签名默认已改为 None(运行时取常量, 见 test_model_list_timeout_default_is_wired_to_the_constant);
    # 这里只钉住常量的【值】, 因为它是写进 CHANGELOG / 两份 README / SKILL.md 的那个数。


@pytest.mark.parametrize("model", [123, 3.5, True, ["gpt-5", "gpt-4"], {"a": 1}])
def test_dry_run_survives_non_string_model(tmp_path, monkeypatch, capsys, model):
    """delta B1(预审):`validate_config` 不校验 model 的【类型】,`model: 123` 一路放行。
    预检里 `model in live_ids` 在 list/dict 上 unhashable 崩、`":" in model` 在 int/bool 上
    TypeError —— 把整个 dry-run 掀掉,退出码 0→1,相对 v1.10.0 是功能回退。
    本仓 test_dry_run_accepts_null_fields 早把「字段显式为 null 不得崩」钉成契约,
    非字符串这半边此前没人钉。"""
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: {"live/one"})
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": model}],
           "options": {}}
    moa.dry_run(cfg, "review", "材料", None, 1, model_check=True)     # 不得抛
    out = capsys.readouterr().out
    assert "a(seat A)" in out
    assert "确认无误后去掉 dry-run 正式运行。" in out                  # 后续输出未被打断


def test_model_check_failure_never_blocks_dry_run(monkeypatch, capsys):
    """delta B1 的结构性那一层:spec 承诺「预检绝不阻断 dry-run」,此前那个承诺只覆盖取列表
    那一次网络调用。判定逻辑本身抛任何异常都必须降级成一行,而不是掀掉 dry-run。"""
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: {"live/one"})
    monkeypatch.setattr(moa, "check_model_slugs",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    cfg = {"members": [{"name": "a", "seat": "A", "channel": "api", "model": "live/one"}],
           "options": {}}
    moa.dry_run(cfg, "review", "材料", None, 1, model_check=True)
    out = capsys.readouterr().out
    assert "判定时出错" in out and "RuntimeError" in out
    assert "确认无误后去掉 dry-run 正式运行。" in out


@pytest.mark.parametrize("model,expect", [
    ("openai/gpt-5.6-sol:typo", "unknown"),   # delta D-M1: 曾被洗成 ok
    ("openai/gpt-5.6-sol:nitro:x", "unknown"),  # 双冒号: rsplit 给 "…:nitro", split 会给 "openai/gpt-5.6-sol"
    ("openai/gpt-5.6-sol:", "unknown"),       # 光秃秃的尾随冒号也曾被洗成 ok
    (":lead", "unknown"),                     # 前导冒号: 基座 id 为空, 不得给出「基座 id 可能是 」空提示
    ("openai/gpt-5.6-sol", "ok"),
    ("live/one:free", "ok"),                  # 真实的含冒号 id 走精确匹配, 不受影响
])
def test_suffix_forms_are_not_washed_into_ok(model, expect):
    """假阴性比假警报更糟:它让用户以为查过了。当天 445 条里含冒号的 96 条只有 :batch/:free
    两种后缀且都是真实 id(精确匹配即可),那条回落兜不到任何真实形态。"""
    live = {"openai/gpt-5.6-sol", "live/one:free"}
    got = moa.check_model_slugs(
        {"members": [{"name": "a", "seat": "A", "channel": "api", "model": model}]}, live)
    assert got[0][1] == expect
    base = model.rsplit(":", 1)[0] if ":" in model else ""
    if expect == "unknown" and base:
        # 删掉后缀回落之后, 这句提示是留给用户的全部补偿(delta-2 L4)
        assert f"基座 id 可能是 {base}" in got[0][2]
    elif ":" in model:
        assert "基座 id 可能是" not in got[0][2]     # 空基座不给无意义提示


def test_fallback_numbering_points_at_the_right_link():
    """delta D-L1: 编号是用户把告警对回 config 里第几条 fallback 的唯一线索,
    出厂 A 席有两条 fallback,差一位就指错。"""
    member = {"name": "a", "seat": "A", "channel": "api", "model": "live/one",
              "fallback": [{"model": "gone/1"}, {"model": "gone/2"}]}
    labels = [lbl for lbl, _st, _d in moa.check_model_slugs({"members": [member]}, {"live/one"})]
    assert labels == ["a(seat A)", "a(seat A) fallback#1", "a(seat A) fallback#2"]


def test_skip_message_quotes_the_real_timeout(monkeypatch, capsys):
    """delta D-L2: 四处 prose 里的 5s 是硬写的,只有这一行会跟着常量走;它一旦也被写死,
    _MODEL_LIST_TIMEOUT 就彻底没有下游了。"""
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: None)
    monkeypatch.setattr(moa, "_MODEL_LIST_TIMEOUT", 7)
    moa.dry_run({"members": [{"name": "a", "channel": "api", "model": "m"}], "options": {}},
                "review", "材料", None, 1, model_check=True)
    assert "7s" in capsys.readouterr().out


def test_model_list_timeout_default_is_wired_to_the_constant(monkeypatch):
    """delta-2 L5: 断 `default == 5` 只钉住【值】;断 `default is _MODEL_LIST_TIMEOUT` 也不行——
    小整数驻留让 `5 is 5` 恒真。改常量后看实际生效的超时,才是真接线。"""
    seen = {}

    class _Resp:
        def read(self): return b'{"data": [{"id": "x/y"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _open(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(moa, "_opener_for", lambda url: types.SimpleNamespace(open=_open))
    monkeypatch.setattr(moa, "_MODEL_LIST_TIMEOUT", 9)
    assert moa.fetch_openrouter_models() == {"x/y"}   # 走成功路径, 否则只证明了失败路径
    assert seen["timeout"] == 9


@pytest.mark.parametrize("cfg,expect_labels", [
    ({"members": ["不是 dict", {"name": "a", "seat": "A", "channel": "api", "model": "m"}]},
     ["a(seat A)"]),                                   # 非 dict 的 member 静默跳过
    ({"members": [{"name": "a", "seat": "A", "channel": "api", "model": "m",
                   "fallback": ["不是 dict", {"model": "n"}]}]},
     ["a(seat A)", "a(seat A) fallback#2"]),           # 非 dict 的 fallback 元素静默跳过
])
def test_expand_links_skips_malformed_entries(cfg, expect_labels):
    """delta-2 L6: 两条防御分支此前从未被任何用例执行过(行覆盖实测)。
    `validate_config` 会拦掉这些形状, 但 check_model_slugs 也被直接调用, 守卫要自证。"""
    assert [lbl for lbl, _s, _d in moa.check_model_slugs(cfg, {"m", "n"})] == expect_labels


def test_model_check_prints_nothing_when_there_are_no_links(monkeypatch, capsys):
    """delta-2 L6 第三条: rows 为空时直接返回, 不打空标题。"""
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: {"m"})
    moa.dry_run({"members": [], "options": {}}, "review", "材料", None, 1, model_check=True)
    out = capsys.readouterr().out
    assert "model 预检(" not in out
    assert "确认无误后去掉 dry-run 正式运行。" in out


def test_render_model_check_failure_is_also_fail_soft(monkeypatch, capsys):
    """delta-2 L2: 外壳此前只裹判定, 打印段在壳外 —— 一个不在 mark 映射里的状态就能
    KeyError 掀掉 dry-run。今天没有 config 能走到, 但 spec 承诺的是结构性的"绝不阻断"。"""
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: {"m"})
    monkeypatch.setattr(moa, "check_model_slugs", lambda *a, **k: [("lbl", "bogus-status", "d")])
    moa.dry_run({"members": [{"name": "a", "channel": "api", "model": "m"}], "options": {}},
                "review", "材料", None, 1, model_check=True)
    out = capsys.readouterr().out
    assert "判定时出错" in out and "KeyError" in out
    assert "确认无误后去掉 dry-run 正式运行。" in out


def test_model_check_header_states_the_comparison_scope(monkeypatch, capsys):
    """delta-3 LOW: `live_count` 的接线没有用例 —— 标题行丢掉「对 N 个…比对」不会被发现,
    而那是用户判断"这次比对到底覆盖了多少"的唯一依据。"""
    monkeypatch.setattr(moa, "fetch_openrouter_models", lambda **k: {"a/1", "b/2", "c/3"})
    moa.dry_run({"members": [{"name": "a", "seat": "A", "channel": "api", "model": "a/1"}],
                 "options": {}}, "review", "材料", None, 1, model_check=True)
    out = capsys.readouterr().out
    assert "对 3 个 OpenRouter 在线模型比对" in out
