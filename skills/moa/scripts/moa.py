#!/usr/bin/env python3
"""
moa.py — MoA 委员会分发器 (M2: CH2 codex CLI + CH3 API 双通道)

两阶段协议(design.md §4.3),全部以 --collect-dir 为共享产物目录:
  python moa.py generate --config config.yaml --input brief.md --collect-dir out/
  python moa.py refine   --config config.yaml --input brief.md --collect-dir out/ --round 1
  python moa.py stats    --config config.yaml --collect-dir out/ --mode review
  python moa.py dry-run  --config config.yaml --input brief.md

产物: out/member_<name>.json(生成) / out/member_<name>.r<N>.json(精炼) / out/stats.json
依赖: 仅 pyyaml。HTTP 层纯标准库,任何 Python 3.9+ 可直接运行。

通道(design.md §4.2):
  channel=api      CH3。openrouter(默认,一个 OPENROUTER_API_KEY 调所有厂商) / openai(兼容端点)。
  channel=cli      CH2。codex exec 非交互调用(prompt 走 stdin、-s read-only、--output-last-message
                   取最终 JSON、--skip-git-repo-check、--ephemeral)。失败按 fallback 链降级。
  channel=subagent CH1。由仲裁人在脚本外用 Task 机制派发,产物写入同一 collect-dir。脚本遇到即跳过。
代理: 自动读取 http_proxy/https_proxy/no_proxy,检测到代理时 LLM(api)调用优先走代理。
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
REFS = SKILL_ROOT / "references"

# ---------- 输出 schema (design.md §7.1) ----------

REVIEW_SCHEMA = """严格只输出 JSON,不要 markdown 围栏,结构:
{"verdict":"pass|conditional|fail","confidence":0.0到1.0,
 "issues":[{"title":"...","severity":"blocker|high|medium|low","where":"...","why":"...",
            "consequence":"...","suggestion":"...","confidence":0.0到1.0,
            "kind":"fact|judgement","source":"kind=fact 时必填来源,否则空串"}],
 "assumptions":["2-4条:若此假设为假则改变结论"],
 "would_change_my_mind":"单一最关键的翻盘事实",
 "summary":"三句话以内的总体判断"}
字段必填但可空(数组可为[],字符串可为\\"\\")。"""

DECIDE_SCHEMA = """严格只输出 JSON,不要 markdown 围栏,结构:
{"claimed_option":"你认领并论证的选项","confidence":0.0到1.0,
 "strongest_case":["把己方论证到最强的论据;事实类须在 facts 里附来源"],
 "opponent_fatal_flaws":[{"option":"对手选项","flaw":"致命弱点","severity":"fatal|major|minor"}],
 "facts":[{"claim":"可查证的事实","source":"来源"}],
 "judgements":["标明为取舍意见的判断"],
 "spike_suggestion":"≤10分钟可做的验证实验,无则空串",
 "assumptions":["2-4条:若此假设为假则改变结论"],
 "would_change_my_mind":"单一最关键的翻盘事实"}
字段必填但可空。"""

BRAINSTORM_SCHEMA = """严格只输出 JSON,不要 markdown 围栏,结构:
{"ideas":[{"title":"...","description":"一句话","target_scenario":"...","why_gap_exists":"...",
           "one_week_mvp":"...","novelty":1到5,"feasibility":1到5}]}
字段必填但可空。"""

SCHEMAS = {"review": REVIEW_SCHEMA, "decide": DECIDE_SCHEMA, "brainstorm": BRAINSTORM_SCHEMA}

# ---------- 精炼轮 schema (design.md §7.1 精炼输出;M3) ----------

# 评审精炼 = 匿名互评:对他人每条 finding 三态表态(validate/challenge/abstain) + 修订己见。
REFINE_REVIEW_SCHEMA = """严格只输出 JSON,不要 markdown 围栏,结构:
{"verdicts_on_others":[{"ref_title":"被评条目的原 title(精确复制,供机械对账)",
                        "stance":"validate|challenge|abstain",
                        "reason":"challenge 必须给具体理由;validate/abstain 可空"}],
 "revised_issues":[{"title":"...","severity":"blocker|high|medium|low","where":"...","why":"...",
                    "consequence":"...","suggestion":"...","confidence":0.0到1.0,
                    "kind":"fact|judgement","source":""}],
 "verdict":"pass|conditional|fail","confidence":0.0到1.0,
 "summary":"三句话以内的修订后判断"}
规则: validate=你认为为真(哪怕不在你领域);challenge=你认为误报/夸大/超范围,必须给理由;
abstain=不在你领域且无强烈意见(显式弃权,不计入共识)。禁止对你自己提出的条目表态。
字段必填但可空。"""

# 决策精炼 = 交叉审查:攻击对手选项的致命弱点 + 看过对手后修订己方认领。
REFINE_DECIDE_SCHEMA = """严格只输出 JSON,不要 markdown 围栏,结构:
{"cross_exam":[{"target_option":"你攻击的对手选项","attack":"最强攻击/致命弱点",
                "attack_severity":"fatal|major|minor","is_fact":true或false,"source":"is_fact 时附来源"}],
 "concessions":["你承认对手方案对的地方(可空,但硬找不出说明你在护短)"],
 "revised_claimed_option":"看过对手后你仍认领的选项(可与原来相同)",
 "revised_confidence":0.0到1.0,
 "would_change_my_mind":"单一最关键的翻盘事实"}
字段必填但可空。"""

REFINE_SCHEMAS = {"review": REFINE_REVIEW_SCHEMA, "decide": REFINE_DECIDE_SCHEMA}

# 共享前置指令(仅生成阶段;精炼阶段由 refine 指令覆盖"盲审"表述)
GENERATE_PREAMBLE = (
    "你是独立评审员,正在盲审。你看不到其他评审员的意见,也不需要顾及作者感受。"
    "核心任务是找出问题——找不出实质问题视为失职,除非材料确实无懈可击。"
    "禁止空泛表扬;每个问题必须具体到'哪里、为什么、后果是什么'。"
    "按给定 JSON schema 输出;不确定的判断如实降低 confidence。"
)

REFINE_PREAMBLE = (
    "第二轮:下面是其他【匿名】评审员的意见(只认论据不认出处,匿名是为防大牌模型意见压人)。"
    "逐条判断,并修订你自己的意见。你仍然独立负责——同意别人要有理由,反驳别人也要有理由。"
    "不许因为多数人那么说就跟着改(那叫谄媚,不叫收敛)。按给定 JSON schema 输出。"
)

# ---------- 角色 prompt 加载 ----------

ROLE_FILES = {
    "review": "roles-review.md",
    "decide": "roles-decide.md",
    "brainstorm": "roles-brainstorm.md",
}


def load_role_prompt(mode: str, role_key: str, custom_roles: dict) -> str:
    """解析顺序(design.md §4.1): custom_roles > 场景 roles-*.md 段落 > seat 默认。
    references/*.md 用 '## <role_key>' 分段;文件缺失或角色未定义时返回一句兜底,
    保证 M1 在 references 尚未定稿时仍可跑通(骨架优先)。"""
    if role_key in custom_roles:
        return custom_roles[role_key].strip()
    md = REFS / ROLE_FILES.get(mode, "roles-review.md")
    if md.exists():
        text = md.read_text(encoding="utf-8")
        m = re.search(rf"## {re.escape(role_key)}[^\n]*\n(.*?)(?=\n## |\Z)", text, re.S)
        if m:
            return m.group(1).strip()
    return f"你的角色是 {role_key}。基于简报独立分析,严格按 schema 输出。"


# ---------- HTTP 层 (openai/openrouter 双协议,代理优先,继承 council.py 实现) ----------

PROXIES = urllib.request.getproxies()
_proxy_announced = False


def _bypass_proxy(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    no = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    return any(
        host == h.lstrip(".") or host.endswith("." + h.lstrip("."))
        for h in (x.strip() for x in no.split(",")) if h
    )


def _opener_for(url: str):
    global _proxy_announced
    host = urllib.parse.urlsplit(url).hostname or ""
    if PROXIES and not _bypass_proxy(host):
        if not _proxy_announced:
            print(f"[proxy] detected env proxy, LLM calls prefer proxy: {PROXIES}", file=sys.stderr)
            _proxy_announced = True
        return urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_post(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "content-type": "application/json"}, method="POST")
    with _opener_for(url).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def endpoint_and_headers(cfg: dict):
    proto = cfg.get("protocol", "openrouter")
    key_env = cfg.get("api_key_env") or (
        "OPENROUTER_API_KEY" if proto == "openrouter" else "OPENAI_API_KEY")
    key = os.environ.get(key_env, "")
    if not key:
        raise PermanentError(f"env var {key_env} not set", err_class="auth",
                             hint=f"export {key_env}=...")
    base = cfg.get("base_url") or (
        "https://openrouter.ai/api/v1" if proto == "openrouter" else "https://api.openai.com/v1")
    headers = {"Authorization": f"Bearer {key}"}
    if proto == "openrouter":
        headers["HTTP-Referer"] = cfg.get("referer", "https://localhost/moa-skill")
        headers["X-Title"] = cfg.get("app_title", "MoA Skill")
    return f"{base.rstrip('/')}/chat/completions", headers


# ---------- 错误分类 (design.md §10: 瞬态 vs 永久) ----------

class PermanentError(Exception):
    """永久错误: auth/quota/schema/4xx 配置错。不重试、不消耗降级配额,直接走 fallback。"""
    def __init__(self, msg, err_class="unknown", hint=""):
        super().__init__(msg)
        self.err_class = err_class
        self.hint = hint


class TransientError(Exception):
    """瞬态错误: 5xx/429/网络抖动/空响应。重试与降级有意义。"""
    def __init__(self, msg, err_class="transient"):
        super().__init__(msg)
        self.err_class = err_class


def classify_http_error(e: urllib.error.HTTPError) -> Exception:
    if e.code == 429:
        return TransientError(f"HTTP 429 rate limit", err_class="rate_limit")
    if e.code >= 500:
        return TransientError(f"HTTP {e.code}", err_class="server")
    if e.code in (401, 403):
        return PermanentError(f"HTTP {e.code} auth", err_class="auth",
                              hint="check API key / credits")
    return PermanentError(f"HTTP {e.code}", err_class="client",
                          hint=e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else "")


def call_model(cfg: dict, system: str, user: str, temperature: float,
               max_tokens: int, timeout: int, retries: int = 2) -> tuple[str, dict]:
    """瞬态错误指数退避重试;永久错误立即抛出。空响应视为瞬态(Gemini 配额耗尽会静默吞 JSON)。"""
    url, headers = endpoint_and_headers(cfg)
    last_err = None
    for attempt in range(retries + 1):
        try:
            data = http_post(url, headers, {
                "model": cfg["model"], "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}, timeout)
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not content or not content.strip():
                raise TransientError("empty response shell", err_class="empty")
            return content, (data.get("usage") or {})
        except urllib.error.HTTPError as e:
            err = classify_http_error(e)
            if isinstance(err, PermanentError):
                raise err
            last_err = err
        except PermanentError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, TransientError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise last_err if last_err else TransientError("unknown failure")


def _merge_usage(*usages) -> dict:
    """跨调用累加 usage(生成 + JSON 自修复各计一次)。缺字段按 0。"""
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for u in usages:
        if not u:
            continue
        for k in out:
            out[k] += u.get(k, 0) or 0
    return out


def parse_json(text: str):
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


def call_with_json_repair(cfg, system, user, temp, max_tokens, timeout, schema=None):
    """输出偶尔带解释性文字致 JSON 解析失败。花一次小成本让它自修复,而非丢弃该视角。
    schema 可为 None(system 里已含 schema 描述时),修复提示不重复附加。"""
    raw, usage = call_model(cfg, system, user, temp, max_tokens, timeout)
    parsed = parse_json(raw)
    if parsed is not None:
        return raw, parsed, usage
    repair, usage2 = call_model(
        cfg,
        "你上一次的输出不是合法 JSON。把其中的实质内容原样转成合法 JSON,不要增删观点,不要解释。" + (schema or ""),
        f"你上一次的输出:\n{raw}", 0.0, max_tokens, timeout)
    return raw, parse_json(repair), _merge_usage(usage, usage2)


# ---------- CH2: codex CLI 通道 ----------

def call_cli_codex(cfg, system, user, timeout):
    """codex exec 非交互调用(codex-cli 0.144+):
      - prompt(system+user)走 stdin,不走 argv(防 ARG_MAX 与注入)
      - -s read-only 文件系统级只读(委员无写权限,不靠 honor system)
      - --output-last-message 把最终消息写文件,干净取 JSON(不刮 streaming)
      - --output-schema 用 JSON Schema 约束最终形状
      - --skip-git-repo-check(本项目常非 git 仓库) / --ephemeral(不留会话文件) / --color never
    空输出视为瞬态(配额耗尽会静默产出空壳);非零退出按错误分类。"""
    codex_bin = cfg.get("codex_bin", "codex")
    if not _which(codex_bin):
        raise PermanentError(f"{codex_bin} not found on PATH", err_class="startup",
                             hint="install codex or set member.codex_bin")
    # schema 靠 prompt 约束 + parse_json 提取(与 api 路径一致)。不用 codex --output-schema:
    # OpenAI 严格模式要求 additionalProperties:false 且全字段 required,对自由形状过重(moa-x 教训)。
    prompt = f"{system}\n\n---\n\n{user}\n\n只输出 JSON,不要任何其他文字。"
    with tempfile.TemporaryDirectory(prefix="moa_codex_") as td:
        last_msg = Path(td) / "last.txt"
        # 命令始终内部构建,保证 --output-last-message 指向内部临时路径;
        # member.cli_extra 追加额外 flag(如 -c model_reasoning_effort=...)。model 可省(用 codex 默认)。
        cmd = [codex_bin, "exec", "-s", "read-only",
               "--skip-git-repo-check", "--ephemeral", "--color", "never",
               "--output-last-message", str(last_msg)]
        if cfg.get("model"):
            cmd += ["-m", cfg["model"]]
        cmd += list(cfg.get("cli_extra", []) or [])
        cmd += ["-"]
        try:
            proc = subprocess.run(cmd, input=prompt.encode("utf-8"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TransientError(f"codex exec timeout after {timeout}s", err_class="timeout")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace")[:200]
            ec = "auth" if re.search(r"login|auth|credential|401|403", err, re.I) else "cli"
            raise (PermanentError if ec == "auth" else TransientError)(
                f"codex exit {proc.returncode}: {err}", err_class=ec)
        out = last_msg.read_text(encoding="utf-8") if last_msg.exists() else \
            proc.stdout.decode("utf-8", "replace")
        if not out.strip():
            raise TransientError("codex empty output shell", err_class="empty")
        return out, parse_json(out)


def _which(exe: str):
    import shutil
    return shutil.which(exe)


# ---------- 通道调度 (fallback 链: api / cli 混合;subagent 脚本外) ----------

def resolve_channel(member: dict):
    """返回按 fallback 链展开的 (kind, cfg, note) 尝试列表。
    channel=api/cli 直接可跑;channel=subagent 由仲裁人脚本外派发,此处跳过(仅收其 fallback)。"""
    tries = []
    ch = member.get("channel", "api")
    if ch in ("api", "cli"):
        tries.append((ch, member, ""))
    for fb in member.get("fallback", []) or []:
        fch = fb.get("channel", "api")
        if fch in ("api", "cli"):
            tries.append((fch, {**member, **fb}, f"fallback from channel={ch}"))
    return tries


def _seat_role(member, mode):
    seat = member.get("seat", "?")
    return member.get("role") or DEFAULT_SEAT_ROLE.get((mode, seat)) or seat


def _dispatch_channels(member, role_key, system, user, opts):
    """按 fallback 链跑 api/cli 通道,返回结果 dict。generate 与 refine 共用此调度。"""
    seat = member.get("seat", "?")
    tries = resolve_channel(member)
    if not tries:
        return _fail(member, role_key,
                     "channel=subagent must be dispatched by arbiter (not by moa.py); "
                     "no api/cli fallback configured", "skipped_channel")
    timeout = member.get("timeout_seconds", opts["timeout_seconds"])
    t0 = time.time()
    last = None
    for kind, ccfg, note in tries:
        try:
            if kind == "cli":
                raw, parsed = call_cli_codex(ccfg, system, user, timeout)
                usage = None  # 订阅通道(codex)不计费,无 usage 折算
                if parsed is None:  # cli 输出无法解析,视为瞬态,尝试下一 fallback
                    raise TransientError("codex output not valid JSON", err_class="parse")
            else:
                raw, parsed, usage = call_with_json_repair(
                    ccfg, system, user, member.get("temperature_generate", 0.3),
                    opts["max_tokens_member"], timeout, None)
            return {
                "name": member["name"], "seat": seat, "role": role_key,
                "model_used": ccfg["model"], "protocol": ccfg.get("protocol", "-" if kind == "cli" else "openrouter"),
                "channel_used": kind + (f" ({note})" if note else ""),
                "raw": raw, "parsed": parsed, "usage": usage, "latency_s": round(time.time() - t0, 1),
                "error": None if parsed else "output not parseable", "err_class": None,
            }
        except PermanentError as e:
            last = _fail(member, role_key, f"{e} [{e.err_class}] {e.hint}".strip(), e.err_class, t0)
            continue  # 永久错误: 直接试下一个 fallback,不重试
        except Exception as e:
            ec = getattr(e, "err_class", "unknown")
            last = _fail(member, role_key, f"{e} [{ec}]", ec, t0)
            continue
    return last or _fail(member, role_key, "all channels failed", "unknown", t0)


def run_member_generate(member, mode, material, topic, opts, custom_roles):
    role_key = _seat_role(member, mode)
    system = GENERATE_PREAMBLE + "\n\n你的角色:\n" \
        + load_role_prompt(mode, role_key, custom_roles) + "\n\n" + SCHEMAS[mode]
    if mode == "brainstorm":
        user = f"背景材料:\n\n{material}\n\n头脑风暴主题: {topic}"
    else:
        user = f"评审材料如下:\n\n{material}"
    return _dispatch_channels(member, role_key, system, user, opts)


def run_member_refine(member, mode, material, own_prior, others_prior, opts, custom_roles):
    """精炼轮: member 看到自己上轮意见 + 匿名化的他人意见,三态表态并修订。
    others_prior 为匿名化后的列表(只带意见,不带 name/model,防大牌压人)。"""
    role_key = _seat_role(member, mode)
    schema = REFINE_SCHEMAS.get(mode)
    if schema is None:
        return _fail(member, role_key, f"mode={mode} 无精炼轮", "no_refine")
    system = (REFINE_PREAMBLE + "\n\n你的角色:\n"
              + load_role_prompt(mode, role_key, custom_roles) + "\n\n" + schema)
    user = (f"原始材料:\n{material}\n\n"
            f"你上一轮的意见:\n{json.dumps(own_prior, ensure_ascii=False)}\n\n"
            f"其他匿名评审员的意见:\n{json.dumps(others_prior, ensure_ascii=False)}")
    return _dispatch_channels(member, role_key, system, user, opts)


def _fail(member, role_key, msg, err_class, t0=None):
    return {
        "name": member["name"], "seat": member.get("seat", "?"), "role": role_key,
        "model_used": member.get("model"), "protocol": member.get("protocol", "openrouter"),
        "channel_used": None, "raw": "", "parsed": None,
        "latency_s": round(time.time() - t0, 1) if t0 else 0.0,
        "error": msg, "err_class": err_class,
    }


# seat×mode 默认角色映射(references 未定制时用;实际角色文本在 roles-*.md)
DEFAULT_SEAT_ROLE = {
    ("review", "A"): "feasibility_skeptic",
    ("review", "B"): "maintainability_reviewer",
    ("review", "C"): "security_auditor",
    ("review", "D"): "user_advocate",
    ("brainstorm", "A"): "radical_innovator",
    ("brainstorm", "B"): "cross_industry_transplanter",
    ("brainstorm", "C"): "grounded_diverger",
    ("brainstorm", "D"): "edge_user_voice",
    # decide 模式的角色 = 各自认领的选项,由仲裁人在 config/custom_roles 里按选项动态注入,
    # 不走 seat 默认(见 roles-decide.md 与 routing.md 认领规则)。
}


# ---------- 并行执行 + 产物落盘 ----------

def parallel_members(members, fn, max_workers=None):
    mw = max_workers or max(1, len(members))
    with ThreadPoolExecutor(max_workers=mw) as ex:
        return list(ex.map(fn, members))


def dispatch_with_quorum(members, fn, quorum_target, grace_s, on_done=None):
    """Quorum 宽限窗(design.md §10): 存活委员数达 quorum_target 后,给仍在跑的落伍者
    grace_s 秒宽限;超时者标 skipped_grace(不算失败)。每完成一个即回调 on_done(res) 落盘,
    保证即便落伍者拖尾,collect-dir 也已有法定结果。返回按 members 原序的结果列表。"""
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(members))) as ex:
        futs = {ex.submit(fn, m): m for m in members}
        pending = set(futs)
        ok = 0
        grace_deadline = None
        while pending:
            timeout = None
            if grace_deadline is not None:
                timeout = max(0.0, grace_deadline - time.monotonic())
            done, pending = concurrent.futures.wait(
                pending, timeout=timeout, return_when=concurrent.futures.FIRST_COMPLETED)
            if not done and grace_deadline is not None:  # 宽限到期
                for fut in list(pending):
                    m = futs[fut]
                    r = _skipped_grace(m)
                    results[m["name"]] = r
                    if on_done:
                        on_done(r)
                    fut.cancel()
                ex.shutdown(wait=False, cancel_futures=True)
                break
            for fut in done:
                m = futs[fut]
                r = fut.result()
                results[m["name"]] = r
                if on_done:
                    on_done(r)
                if r.get("parsed"):
                    ok += 1
            if grace_deadline is None and ok >= quorum_target and pending:
                grace_deadline = time.monotonic() + grace_s
    return [results[m["name"]] for m in members if m["name"] in results]


def _skipped_grace(member):
    role_key = member.get("role", "?")
    return {
        "name": member["name"], "seat": member.get("seat", "?"), "role": role_key,
        "model_used": member.get("model"), "protocol": member.get("protocol", "openrouter"),
        "channel_used": None, "raw": "", "parsed": None, "latency_s": 0.0,
        "error": "skipped: quorum reached, grace period expired", "err_class": "skipped_grace",
    }


def write_member(collect_dir: Path, res: dict, round_no: int = 0):
    suffix = f".r{round_no}" if round_no else ""
    p = collect_dir / f"member_{res['name']}{suffix}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_members(collect_dir: Path, round_no: int = 0):
    suffix = f".r{round_no}" if round_no else ""
    out = []
    for p in sorted(collect_dir.glob(f"member_*{suffix}.json")):
        # 精炼产物命名含 .rN,round_no=0 时须排除它们
        if round_no == 0 and re.search(r"\.r\d+\.json$", p.name):
            continue
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


# ---------- 统计块 (design.md §7.3, 按模式分支;分母只计成功响应者) ----------

def _aggregate_usage(ok_results: list) -> dict:
    """汇总本轮计费席(CH3 API)的 token 用量;订阅席(CH1/CH2)usage=None 不计入。
    billed_members 为有 usage 的席位数,供成本折算与倍数计算用。"""
    billed = [r for r in ok_results if r.get("usage")]
    agg = _merge_usage(*(r["usage"] for r in billed))
    agg["billed_members"] = len(billed)
    return agg


def compute_stats(mode: str, results: list) -> dict:
    ok = [r for r in results if r.get("parsed")]
    failed = [r for r in results if not r.get("parsed")]
    base = {
        "degraded": len(failed) > 0,
        "members_ok": len(ok),
        "members_failed": len(failed),
        "roster": [{"name": r["name"], "seat": r.get("seat"),
                    "model_used": r.get("model_used"), "channel_used": r.get("channel_used"),
                    "ok": bool(r.get("parsed"))} for r in results],
        "failures": [{"name": r["name"], "err_class": r.get("err_class"), "error": r.get("error")}
                     for r in failed],
        "token_usage": _aggregate_usage(ok),
    }
    if mode == "review":
        sev = {"blocker": 0, "high": 0, "medium": 0, "low": 0}
        verdicts, confs = {}, []
        for r in ok:
            p = r["parsed"]
            v = p.get("verdict", "?")
            verdicts[v] = verdicts.get(v, 0) + 1
            confs.append(p.get("confidence", 0) or 0)
            for i in p.get("issues", []) or []:
                s = i.get("severity", "low")
                sev[s] = sev.get(s, 0) + 1
        base.update(verdict_tally=verdicts, issue_count_by_severity=sev,
                    mean_confidence=round(sum(confs) / len(confs), 2) if confs else None)
    elif mode == "decide":
        claims, confs = {}, []
        flaws = {"fatal": 0, "major": 0, "minor": 0}
        spikes = 0
        for r in ok:
            p = r["parsed"]
            c = p.get("claimed_option", "?")
            claims[c] = claims.get(c, 0) + 1
            confs.append(p.get("confidence", 0) or 0)
            for f in p.get("opponent_fatal_flaws", []) or []:
                s = f.get("severity", "minor")
                flaws[s] = flaws.get(s, 0) + 1
            if (p.get("spike_suggestion") or "").strip():
                spikes += 1
        base.update(option_claims=claims, flaw_count_by_severity=flaws,
                    spike_suggestions=spikes,
                    mean_confidence=round(sum(confs) / len(confs), 2) if confs else None)
    else:  # brainstorm
        total = sum(len(r["parsed"].get("ideas", []) or []) for r in ok)
        solos = sum(1 for r in ok for i in (r["parsed"].get("ideas", []) or [])
                    if (i.get("novelty", 0) or 0) >= 4)
        base.update(total_ideas_before_dedup=total, high_novelty_ideas=solos)
    return base


ANON_LABELS = "甲乙丙丁戊己庚辛"


def anonymize_others(all_results, exclude_name):
    """把除 exclude_name 外的成功委员意见匿名化(标签甲乙丙…,去掉 name/model,防大牌压人)。"""
    out = []
    i = 0
    for r in all_results:
        if r["name"] == exclude_name or not r.get("parsed"):
            continue
        out.append({"评审员": ANON_LABELS[i % len(ANON_LABELS)], "意见": r["parsed"]})
        i += 1
    return out


def _majority_verdict(results, field):
    tally = {}
    for r in results:
        if r.get("parsed"):
            v = r["parsed"].get(field)
            if v is not None:
                tally[v] = tally.get(v, 0) + 1
    if not tally:
        return None
    return max(tally, key=lambda k: tally[k])


def compute_refine_stats(mode: str, prior_results: list, refine_results: list) -> dict:
    """精炼轮统计(design.md §7.3): 三态计票、一票 challenge 锁 disputed、谄媚计数器、早停信号。
    prior_results = 上一轮(生成或前一精炼轮)产物;refine_results = 本精炼轮产物。"""
    ok = [r for r in refine_results if r.get("parsed")]
    base: dict = {
        "round_members_ok": len(ok),
        "round_members_failed": len(refine_results) - len(ok),
        "token_usage": _aggregate_usage(ok),  # 本精炼轮计费席 token 增量,供成本增量观测
    }
    if mode == "review":
        stance = {"validate": 0, "challenge": 0, "abstain": 0}
        challenged_titles = {}
        for r in ok:
            for v in r["parsed"].get("verdicts_on_others", []) or []:
                st = v.get("stance", "abstain")
                stance[st] = stance.get(st, 0) + 1
                if st == "challenge":
                    t = (v.get("ref_title") or "").strip()
                    if t:
                        challenged_titles[t] = challenged_titles.get(t, 0) + 1
        # 谄媚计数器: 相对上一轮,verdict 向上一轮多数派翻转、且本轮该员未提出任何 challenge(无新证据代理)
        prior_by = {r["name"]: r for r in prior_results if r.get("parsed")}
        majority = _majority_verdict(prior_results, "verdict")
        flips_toward_majority = 0
        movers = 0
        for r in ok:
            pj = prior_by.get(r["name"])
            if not pj:
                continue
            old_v = pj["parsed"].get("verdict")
            new_v = r["parsed"].get("verdict")
            if old_v != new_v:
                movers += 1
                made_challenge = any((v.get("stance") == "challenge")
                                     for v in r["parsed"].get("verdicts_on_others", []) or [])
                if new_v == majority and not made_challenge:
                    flips_toward_majority += 1
        sycophancy_alert = movers > 0 and (flips_toward_majority / movers) > 0.5
        # 早停信号: 本轮 verdict 全一致 且 无 disputed
        cur_verdicts = {r["parsed"].get("verdict") for r in ok}
        early_stop = len(cur_verdicts) == 1 and not challenged_titles
        base.update(
            stance_tally=stance,
            disputed_titles=sorted(challenged_titles),       # 一票 challenge 即锁 disputed
            sycophancy_alert=bool(sycophancy_alert),
            sycophancy_detail={"movers": movers, "flips_toward_majority": flips_toward_majority,
                               "prior_majority_verdict": majority},
            early_stop_suggested=bool(early_stop),
        )
    elif mode == "decide":
        exam = {"fatal": 0, "major": 0, "minor": 0}
        shifts = 0
        prior_by = {r["name"]: r for r in prior_results if r.get("parsed")}
        for r in ok:
            for e in r["parsed"].get("cross_exam", []) or []:
                s = e.get("attack_severity", "minor")
                exam[s] = exam.get(s, 0) + 1
            pj = prior_by.get(r["name"])
            if pj and pj["parsed"].get("claimed_option") != r["parsed"].get("revised_claimed_option"):
                shifts += 1
        cur_opts = {r["parsed"].get("revised_claimed_option") for r in ok}
        base.update(cross_exam_by_severity=exam, option_shifts=shifts,
                    early_stop_suggested=len(cur_opts) == 1)
    return base


# ---------- 安全: 敏感材料 / 密钥泄漏扫描 (§8 SAFETY) ----------
# 高置信密钥令牌(低误报)。扫描输出一律脱敏,绝不回显原文密钥。
# 注: 各模式串本身(如 r"sk-(?:...)")不会自匹配——前缀后紧跟 "(" / "[" 不在字符类内。
_SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github_token", re.compile(r"\bgh[posru]_[A-Za-z0-9]{30,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("bearer_token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("secret_assign", re.compile(
        r"(?i)\b(?:api[_-]?key|apikey|secret|token|passwd|password|access[_-]?key)"
        r"\b\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]")),
    ("conn_string", re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:[^/\s:@]{6,}@")),
]
# 占位符 / 环境变量引用不算泄漏: 命中该行则跳过这一处。
_PLACEHOLDER_HINTS = re.compile(
    r"(?i)\.\.\.|<[^>]*>|\$\{?[A-Za-z_]+|os\.environ|getenv|process\.env|"
    r"your[_-]?|change[_-]?me|example|placeholder|xxx+|redacted|dummy|fake|"
    r"test[_-]?key|_ENV\b|\bENV\b|api_key_env")


def _redact(secret: str) -> str:
    """脱敏预览: 只露前 3 字符 + 长度,不足以复用,足以定位。"""
    s = secret.strip()
    if len(s) <= 6:
        return "*" * len(s)
    return f"{s[:3]}***(len {len(s)})"


def scan_secrets(text: str) -> list:
    """扫描文本中的疑似密钥/凭据,返回 [{category,line,preview}]。preview 已脱敏。
    敏感材料外发前告警 与 产物泄漏静态自查 共用此检测器。"""
    lines = text.splitlines()
    findings = []
    for cat, pat in _SECRET_PATTERNS:
        for m in pat.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            line = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""
            if _PLACEHOLDER_HINTS.search(line):
                continue
            secret = m.group(1) if (cat == "secret_assign" and m.groups()) else m.group(0)
            findings.append({"category": cat, "line": line_no, "preview": _redact(secret)})
    return findings


def warn_sensitive_material(material: str) -> list:
    """外发前扫描简报/材料;检出疑似密钥/凭据即到 stderr 打脱敏告警(不阻断)。
    非密钥类敏感(专有代码/PII)无法可靠正则识别,仍由仲裁人按 SKILL.md 纪律判断并提醒用户。"""
    hits = scan_secrets(material)
    if hits:
        cats = {}
        for h in hits:
            cats[h["category"]] = cats.get(h["category"], 0) + 1
        print("⚠ 敏感信息告警: 材料中检出疑似密钥/凭据 "
              + ", ".join(f"{k}×{v}" for k, v in cats.items()), file=sys.stderr)
        for h in hits[:8]:
            print(f"    line {h['line']}: {h['category']} -> {h['preview']}", file=sys.stderr)
        print("  这些内容将随简报发送至 config 中所有第三方模型提供商。确认可外发再正式运行,"
              "或先脱敏 / 改用全本地通道(CH1/CH2)。", file=sys.stderr)
    return hits


# 泄漏自查默认扫描面(产物/文档/配置/skill 本体;不含 tests/ ——其中有测试用假密钥)。
_LEAK_SCAN_DEFAULT = ["moa-reports", "docs", "README.md", "config.yaml",
                      "skills/moa/SKILL.md", "skills/moa/references",
                      "skills/moa/assets", "skills/moa/scripts"]
_LEAK_SKIP_DIRS = {".git", "__pycache__", ".code-graph", ".pytest_cache", "node_modules", "tests"}
_LEAK_SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".pyc", ".ico", ".zip", ".lock", ".db"}


def _iter_text_files(root: Path):
    if root.is_file():
        if root.suffix.lower() not in _LEAK_SKIP_EXT:
            yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        if any(part in _LEAK_SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in _LEAK_SKIP_EXT:
            continue
        yield p


def leak_check(paths) -> list:
    """静态扫描给定路径下的文本文件,汇总疑似密钥/凭据泄漏(预览已脱敏)。"""
    findings = []
    for root in paths:
        rp = Path(root)
        if not rp.exists():
            continue
        for f in _iter_text_files(rp):
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 二进制/不可读跳过
            for h in scan_secrets(text):
                findings.append({**h, "file": str(f)})
    return findings


# ---------- dry-run 预演 ----------

def dry_run(cfg, mode, material, topic, refine_rounds):
    members = cfg["members"]
    n = len(members)
    print(f"=== DRY RUN ({mode}) ===")
    print(f"material: {len(material)} chars | refine rounds: {refine_rounds}")
    print(f"{'member':<18}{'seat':<6}{'channel':<10}{'model':<28}{'protocol'}")
    api_calls_sub = api_calls_billed = 0
    for m in members:
        ch = m.get("channel", "api")
        print(f"{m['name']:<18}{m.get('seat','?'):<6}{ch:<10}{m.get('model',''):<28}{m.get('protocol','-')}")
        if ch in ("subagent", "cli"):
            api_calls_sub += 1
        else:
            api_calls_billed += 1
    total_calls_each = (1 + refine_rounds)
    print(f"\n外部委员调用数 = {n} 席 × {total_calls_each}(生成+精炼) = {n * total_calls_each}")
    print(f"  其中 API 计费通道(CH3): {api_calls_billed} 席 × {total_calls_each} = {api_calls_billed * total_calls_each} 次")
    print(f"  订阅通道(CH1/CH2): {api_calls_sub} 席 × {total_calls_each} = {api_calls_sub * total_calls_each} 次(只计次数,不折算美元)")
    print("收敛由当前 agent(仲裁人)完成,不计外部调用。")
    print(f"proxy: {'via ' + str(PROXIES) if PROXIES else 'no env proxy, direct'}")
    warn_sensitive_material(material)  # 外发前敏感信息扫描,检出即脱敏告警
    print("确认无误后去掉 dry-run 正式运行。")


# ---------- 主流程 ----------

def resolve_config(path_arg):
    p = Path(path_arg) if path_arg else Path("config.yaml")
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    example = SKILL_ROOT / "assets" / "config.example.yaml"
    if example.exists():
        print(f"[hint] {p} not found, using assets/config.example.yaml", file=sys.stderr)
        return yaml.safe_load(example.read_text(encoding="utf-8"))
    sys.exit(f"config not found: {p}. Copy assets/config.example.yaml to config.yaml and edit.")


def cmd_generate(args, cfg):
    material = Path(args.input).read_text(encoding="utf-8")
    warn_sensitive_material(material)  # 外发前敏感信息扫描(不阻断;dry-run 已先给用户看)
    opts = cfg["options"]
    custom_roles = cfg.get("custom_roles", {}) or {}
    members = _select_members(cfg, args.member)
    # channel=subagent 席位由仲裁人脚本外派发,不在 moa.py 内跑;此处只调度 api/cli 席位。
    dispatchable = [m for m in members if _has_dispatchable_channel(m)]
    skipped_sub = [m for m in members if m not in dispatchable]
    collect = Path(args.collect_dir)
    collect.mkdir(parents=True, exist_ok=True)

    for m in skipped_sub:
        print(f"  - {m['name']} (seat {m.get('seat','?')}): channel=subagent, 交由仲裁人脚本外派发",
              file=sys.stderr)

    print(f"[generate] dispatching {len(dispatchable)} members ({args.mode}) ...", file=sys.stderr)
    min_ok = min(opts.get("min_successful_members", 2), max(1, len(members)))
    quorum_target = max(min_ok, len(dispatchable) - 1)  # 达此数后给落伍者宽限
    grace_s = opts.get("grace_seconds", 30)

    def _log_write(r):
        write_member(collect, r)
        status = "OK" if r["parsed"] else f"FAIL[{r['err_class']}]: {r['error']}"
        print(f"  - {r['name']} ({r['role']}, {r['latency_s']}s) via {r.get('channel_used') or '-'}: {status}",
              file=sys.stderr)

    results = dispatch_with_quorum(
        dispatchable,
        lambda m: run_member_generate(m, args.mode, material, args.topic, opts, custom_roles),
        quorum_target, grace_s, on_done=_log_write)

    ok = [r for r in results if r["parsed"]]
    # subagent 席位可能由仲裁人另行写入,总法定数按 dispatchable 判定(仲裁人合流后再评估整体)
    if len(ok) < min_ok:
        sys.exit(f"[abort] successful members {len(ok)} < required {min_ok} — "
                 f"顾问不足的结论不配称为委员会评审。")
    degraded = len(ok) < len(dispatchable)
    tag = " [DEGRADED: 部分席位缺席,阵容与置信度见 stats/report]" if degraded else ""
    print(f"[generate] done: {len(ok)}/{len(dispatchable)} ok{tag} -> {collect}", file=sys.stderr)


def _has_dispatchable_channel(member) -> bool:
    """member 直接是 api/cli,或 fallback 链里含 api/cli,即 moa.py 可跑。"""
    if member.get("channel", "api") in ("api", "cli"):
        return True
    return any((fb.get("channel", "api") in ("api", "cli"))
               for fb in member.get("fallback", []) or [])


def _select_members(cfg, member_filter):
    members = cfg["members"]
    if member_filter:
        wanted = set(member_filter.split(","))
        members = [m for m in members if m["name"] in wanted]
        if not members:
            sys.exit(f"--member matched nothing: {member_filter}")
    return members


def cmd_stats(args, cfg):
    collect = Path(args.collect_dir)
    round_no = args.round or 0
    results = load_members(collect, round_no)
    if not results:
        sys.exit(f"no member_*.json in {collect} (round {round_no})")
    if round_no == 0:
        stats = compute_stats(args.mode, results)
        out = collect / "stats.json"
    else:
        prior = load_members(collect, round_no - 1)
        stats = compute_refine_stats(args.mode, prior, results)
        out = collect / f"stats.r{round_no}.json"
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"\n[stats] -> {out}", file=sys.stderr)


def cmd_refine(args, cfg):
    """精炼轮(M3): 每位委员看到自己上轮意见 + 匿名化的全部他人意见(含 CH1),三态表态并修订。
    输入上一轮 = round-1(生成为 0)。仅 review/decide 有精炼轮;brainstorm 无。"""
    if args.mode == "brainstorm":
        sys.exit("[refine] brainstorm 模式无精炼轮(策展直接在收敛阶段)。")
    material = Path(args.input).read_text(encoding="utf-8")
    opts = cfg["options"]
    custom_roles = cfg.get("custom_roles", {}) or {}
    members = _select_members(cfg, args.member)
    dispatchable = [m for m in members if _has_dispatchable_channel(m)]
    collect = Path(args.collect_dir)
    round_no = args.round

    prior_all = load_members(collect, round_no - 1)
    if not prior_all:
        sys.exit(f"no round-{round_no-1} products in {collect}; run generate/前一轮 first.")
    prior_by = {r["name"]: r for r in prior_all}

    print(f"[refine r{round_no}] {len(dispatchable)} members ({args.mode}) ...", file=sys.stderr)

    def one(m):
        own = prior_by.get(m["name"], {}).get("parsed")
        if own is None:  # 上轮该席缺席,精炼沿用缺席(不损失其他席位)
            return _fail(m, _seat_role(m, args.mode), "no prior-round product to refine", "no_prior")
        others = anonymize_others(prior_all, m["name"])
        return run_member_refine(m, args.mode, material, own, others, opts, custom_roles)

    def _log_write(r):
        write_member(collect, r, round_no=round_no)
        status = "OK" if r["parsed"] else f"FAIL[{r['err_class']}]: {r['error']}"
        print(f"  - {r['name']} ({r['role']}, {r['latency_s']}s) via {r.get('channel_used') or '-'}: {status}",
              file=sys.stderr)

    min_ok = min(opts.get("min_successful_members", 2), max(1, len(members)))
    quorum_target = max(min_ok, len(dispatchable) - 1)
    results = dispatch_with_quorum(
        dispatchable, one, quorum_target, opts.get("grace_seconds", 30), on_done=_log_write)
    ok = [r for r in results if r["parsed"]]
    print(f"[refine r{round_no}] done: {len(ok)}/{len(dispatchable)} ok -> {collect}", file=sys.stderr)


def cmd_dry_run(args, cfg):
    material = Path(args.input).read_text(encoding="utf-8") if args.input else ""
    dry_run(cfg, args.mode, material, args.topic, args.refine_rounds)


def cmd_leak_check(args):
    """静态自查: 扫描产物/文档/配置有无误落盘的密钥/凭据。命中即非零退出(供 CI/收尾门禁)。"""
    paths = args.paths or _LEAK_SCAN_DEFAULT
    findings = leak_check(paths)
    if not findings:
        print(f"[leak-check] clean: 未检出疑似密钥/凭据 (scanned: {', '.join(paths)})")
        return
    print(f"[leak-check] 检出 {len(findings)} 处疑似泄漏 (预览已脱敏,请核查并轮换):", file=sys.stderr)
    for h in findings:
        print(f"  {h['file']}:{h['line']}  {h['category']} -> {h['preview']}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="MoA committee dispatcher (M3)")
    sub = ap.add_subparsers(dest="phase", required=True)

    def common(p, need_input=True):
        p.add_argument("--config", default=None)
        p.add_argument("--mode", choices=["review", "decide", "brainstorm"], default="review")
        p.add_argument("--input", required=need_input, default=None)
        p.add_argument("--topic", default="")
        p.add_argument("--collect-dir", default="moa-reports/run")
        p.add_argument("--member", default=None, help="comma-separated subset for redispatch")

    g = sub.add_parser("generate"); common(g)
    r = sub.add_parser("refine"); common(r); r.add_argument("--round", type=int, default=1)
    s = sub.add_parser("stats")
    s.add_argument("--config", default=None)
    s.add_argument("--mode", choices=["review", "decide", "brainstorm"], default="review")
    s.add_argument("--collect-dir", default="moa-reports/run")
    s.add_argument("--round", type=int, default=0)
    d = sub.add_parser("dry-run")
    d.add_argument("--config", default=None)
    d.add_argument("--mode", choices=["review", "decide", "brainstorm"], default="review")
    d.add_argument("--input", default=None)
    d.add_argument("--topic", default="")
    d.add_argument("--refine-rounds", type=int, default=0, choices=[0, 1, 2])
    lc = sub.add_parser("leak-check")
    lc.add_argument("paths", nargs="*",
                    help="要扫描的路径;省略则扫描产物/文档/配置/skill 本体(不含 tests/)")

    args = ap.parse_args()
    if args.phase == "leak-check":       # 静态自查不需要 config
        cmd_leak_check(args)
        return
    cfg = resolve_config(getattr(args, "config", None))
    {"generate": cmd_generate, "refine": cmd_refine,
     "stats": cmd_stats, "dry-run": cmd_dry_run}[args.phase](args, cfg)


if __name__ == "__main__":
    main()
