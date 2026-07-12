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

# ---------- 开会讨论 schema/preamble (§6 阶段5;仅 L3 且用户显式要求) ----------
# 顺序发言、后发者可见此前发言(盲审的显式例外),多轮。头号风险=从众,故每回合强制
# 标注"是否被新论据改变立场";收尾另有一次盲投(不看 transcript)做漂移检测。
DISCUSS_SCHEMA = """严格只输出 JSON,不要 markdown 围栏,结构:
{"still_holding":"你此刻仍坚持的核心立场(一句话)",
 "responses":[{"to":"你回应的委员标签或其某个论点","stance":"agree|rebut|merge","reason":"理由(必填,不许空泛)"}],
 "new_argument":"本轮你提出的、此前发言记录里无人提过的新论据(没有就填空串)",
 "position_changed":true或false,
 "changed_by_new_argument":true或false,
 "current_stance":"你此刻的结论立场","confidence":0.0到1.0}
硬规则:position_changed=true 时,changed_by_new_argument 必须如实——无新论据却随多数改立场=从众(会被机械计数),不是收敛。"""

BLIND_VOTE_SCHEMA = """严格只输出 JSON,不要 markdown 围栏,结构:
{"final_stance":"不看讨论记录,仅凭简报与你自己的判断复述的最终立场",
 "confidence":0.0到1.0,"key_reason":"支撑该立场最关键的一条理由"}
字段必填。"""

DISCUSS_PREAMBLE = (
    "这是【开会讨论】:顺序发言,你能看到此前发言者的发言(这是本模式对盲审原则的显式例外)。"
    "发言只署名到'委员X(角色)',不暴露模型身份——只认论据不认出处。"
    "轮到你时:(1)明确你仍坚持的立场;(2)逐条回应你同意/反驳/合并的他人观点并给理由;"
    "(3)只有当出现你此前没考虑到的【新论据】时才可改变立场,并把 changed_by_new_argument 标 true、"
    "在 new_argument 写清是哪条。无新论据却随多数改立场=从众,不是收敛。按给定 JSON schema 输出。"
)

BLINDVOTE_PREAMBLE = (
    "讨论已结束。现在【不参考任何讨论记录】,仅凭简报和你自己的独立判断,复述你的最终立场。"
    "这是为检测讨论是否让你无理由漂移——若讨论确实用新论据改变了你,如实反映;若没有,坚持你本来的判断。"
    "按给定 JSON schema 输出。"
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
    entries = [x.strip() for x in no.split(",") if x.strip()]
    if "*" in entries:            # NO_PROXY=* → 绕过所有主机(修 C4)
        return True
    return any(
        host == h.lstrip(".") or host.endswith("." + h.lstrip("."))
        for h in entries
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


def _effective_billing(member) -> str:
    """dry-run 计费判定:返回 'billed'(CH3 计费) 或 'sub'(订阅/免费,CH1 子代理或 CH2 codex)。
    按 moa.py *真正会跑* 的通道判定,而非配置的主通道——纯 subagent(无 api/cli fallback)由仲裁人
    免费派发;否则脚本跑 resolve_channel 的首个 try(cli=订阅免费, api=计费)。修正旧逻辑只看主通道、
    把 'subagent + api fallback' 误记为免费的少报 bug(该席位 generate 时实走计费 API)。"""
    tries = resolve_channel(member)
    if not tries:
        return "sub"                        # 纯 subagent → 仲裁人免费派发
    return "sub" if tries[0][0] == "cli" else "billed"


def _seat_role(member, mode):
    seat = member.get("seat", "?")
    return member.get("role") or DEFAULT_SEAT_ROLE.get((mode, seat)) or seat


def _dispatch_channels(member, role_key, system, user, opts, default_temp=0.3):
    """按 fallback 链跑 api/cli 通道,返回结果 dict。generate 与 refine 共用此调度。
    default_temp: 未设 member.temperature_generate 时的默认温度;brainstorm 传更高值发散(P1-4)。"""
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
                    ccfg, system, user, member.get("temperature_generate", default_temp),
                    opts["max_tokens_member"], timeout, None)
            return {
                "name": member["name"], "seat": seat, "role": role_key,
                "model_used": ccfg.get("model"),  # codex 席可省 model(用 codex 默认)→ None,非 KeyError
                "protocol": ccfg.get("protocol", "-" if kind == "cli" else "openrouter"),
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
    # brainstorm 求发散: 未显式设温时默认 0.9(P1-4,兑现 roles-brainstorm.md「高 temperature」);
    # review/decide 求稳定判断,默认 0.3。member.temperature_generate 显式设置仍优先。
    default_temp = 0.9 if mode == "brainstorm" else 0.3
    return _dispatch_channels(member, role_key, system, user, opts, default_temp)


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


# ---------- 开会讨论: 顺序回合 + 收尾盲投(§6 阶段5) ----------

def _speaker_label(turn: dict) -> str:
    """发言署名只到 席位+角色,不暴露模型身份(讨论里也不给'大牌压人'留口子)。"""
    return f"委员{turn.get('seat', '?')}({turn.get('role', '?')})"


def format_transcript(turns: list) -> str:
    """把已落盘的回合(turn 非空)按轮次格式化成可见发言记录。空=首位发言者无记录。"""
    visible = [t for t in turns if t.get("turn")]
    if not visible:
        return "(你是本轮第一位发言者,暂无此前发言。)"
    by_round = {}
    for t in visible:
        by_round.setdefault(t.get("round", 1), []).append(t)
    lines = []
    for rnd in sorted(by_round):
        lines.append(f"—— 第 {rnd} 轮 ——")
        for t in by_round[rnd]:
            p = t["turn"]
            resp = "; ".join(f"[{r.get('stance')}→{r.get('to')}] {r.get('reason', '')}"
                             for r in p.get("responses", []))
            lines.append(f"{_speaker_label(t)}: 立场「{p.get('current_stance', '')}」"
                         + (f" | 新论据: {p['new_argument']}" if p.get("new_argument") else "")
                         + (f" | 回应: {resp}" if resp else ""))
    return "\n".join(lines)


def discuss_prompt(member, mode, material, transcript_str, round_no, custom_roles, blind=False):
    """构造讨论回合(或盲投)的 (system, user)。CH1 子代理由仲裁人用同一 prompt 外派发,保证一致。"""
    role_key = _seat_role(member, mode)
    if blind:
        system = (BLINDVOTE_PREAMBLE + "\n\n你的角色:\n"
                  + load_role_prompt(mode, role_key, custom_roles) + "\n\n" + BLIND_VOTE_SCHEMA)
        user = f"简报:\n\n{material}"
        return system, user
    system = (DISCUSS_PREAMBLE + "\n\n你的角色:\n"
              + load_role_prompt(mode, role_key, custom_roles) + "\n\n" + DISCUSS_SCHEMA)
    user = (f"简报:\n\n{material}\n\n"
            f"此前发言记录:\n{transcript_str}\n\n"
            f"现在轮到你(第 {round_no} 轮)发言。")
    return system, user


def run_member_discuss_turn(member, mode, material, transcript_str, round_no, opts, custom_roles):
    role_key = _seat_role(member, mode)
    system, user = discuss_prompt(member, mode, material, transcript_str, round_no, custom_roles)
    res = _dispatch_channels(member, role_key, system, user, opts)
    res["round"] = round_no
    return res


def run_member_blindvote(member, mode, material, opts, custom_roles):
    role_key = _seat_role(member, mode)
    system, user = discuss_prompt(member, mode, material, "", 0, custom_roles, blind=True)
    return _dispatch_channels(member, role_key, system, user, opts)


def load_transcript(collect_dir: Path) -> list:
    """读 discussion.jsonl(每行一个回合信封);不存在则空。"""
    p = Path(collect_dir) / "discussion.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_transcript(collect_dir: Path, envelope: dict):
    p = Path(collect_dir) / "discussion.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(envelope, ensure_ascii=False) + "\n")


def _turn_envelope(res: dict, round_no: int) -> dict:
    """把 _dispatch_channels/注入结果规约成 transcript 回合信封。"""
    return {
        "round": round_no, "seat": res.get("seat", "?"), "name": res.get("name"),
        "role": res.get("role"), "channel_used": res.get("channel_used"),
        "model_used": res.get("model_used"), "turn": res.get("parsed"),
        "usage": res.get("usage"), "latency_s": res.get("latency_s", 0.0),
        "error": res.get("error"), "err_class": res.get("err_class"),
    }


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

def dispatch_with_quorum(members, fn, quorum_target, grace_s, on_done=None):
    """Quorum 宽限窗(design.md §10): 存活委员数达 quorum_target 后,给仍在跑的落伍者
    grace_s 秒宽限;超时者标 skipped_grace(不算失败)。每完成一个即回调 on_done(res) 落盘,
    保证即便落伍者拖尾,collect-dir 也已有法定结果。返回按 members 原序的结果列表。

    止损语义(修 P0-1): 宽限到期必须【立即交还控制权】,不得 join 落伍线程。此前用
    `with ThreadPoolExecutor` 管理,块退出隐式 shutdown(wait=True) 会 join 全部线程,
    使宽限窗形同虚设(实测 3 席 grace=0.5s 仍 wall=6s)。现改手动管理: 到期
    shutdown(wait=False),仲裁流程即刻拿到法定结果继续。落伍线程受 member 级 timeout
    约束在后台自然了结(不无限拖尾),不因此丢失总流程时间。"""
    results = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(members)))
    abandoned = False
    try:
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
            if not done and grace_deadline is not None:  # 宽限到期: 放弃落伍者, 立即返回
                for fut in list(pending):
                    m = futs[fut]
                    r = _skipped_grace(m)
                    results[m["name"]] = r
                    if on_done:
                        on_done(r)
                    fut.cancel()  # 尚未起跑的能真取消; 已在跑的由 member 级 timeout 自行了结
                abandoned = True
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
    finally:
        # abandoned=True → wait=False 立即交还控制权(不 join 落伍线程); 正常完成 → wait=True。
        ex.shutdown(wait=not abandoned)
    return [results[m["name"]] for m in members if m["name"] in results]


def _skipped_grace(member):
    role_key = member.get("role", "?")
    return {
        "name": member["name"], "seat": member.get("seat", "?"), "role": role_key,
        "model_used": member.get("model"), "protocol": member.get("protocol", "openrouter"),
        "channel_used": None, "raw": "", "parsed": None, "latency_s": 0.0,
        "error": "skipped: quorum reached, grace period expired", "err_class": "skipped_grace",
    }


def _safe_name(name: str) -> str:
    """把 member name 收敛成安全文件名片段(修 C2): 只留字母/数字/._-,其余(含 / 和 ..)
    换成 _,防路径穿越把产物写出 collect-dir。config 由用户自控,风险低,但门要堵。"""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(name))
    s = s.strip(".") or "member"        # 全点/空 → 兜底,避免 '' 或 '..'
    return s


def write_member(collect_dir: Path, res: dict, round_no: int = 0):
    suffix = f".r{round_no}" if round_no else ""
    p = collect_dir / f"member_{_safe_name(res['name'])}{suffix}.json"
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


def compute_discuss_stats(transcript: list, blindvotes: list) -> dict:
    """开会讨论统计: 从众计数(无新论据却改立场) + 假讨论(整轮无新论据) + 盲投漂移对照 + 保留分歧。
    transcript = discussion.jsonl 全部回合;blindvotes = blindvote_<seat>.json 的 parsed 列表(可空)。"""
    ok = [t for t in transcript if t.get("turn")]
    rounds = sorted({t.get("round", 1) for t in ok})
    # 从众: 任一回合 position_changed 却非 changed_by_new_argument
    conformity = []
    for t in ok:
        p = t["turn"]
        if p.get("position_changed") and not p.get("changed_by_new_argument"):
            conformity.append({"seat": t.get("seat"), "role": t.get("role"),
                               "round": t.get("round"), "current_stance": p.get("current_stance")})
    # 假讨论: 整轮所有发言 new_argument 皆空(无信息增量)
    pseudo_rounds = []
    for rnd in rounds:
        turns = [t["turn"] for t in ok if t.get("round") == rnd]
        if turns and all(not (x.get("new_argument") or "").strip() for x in turns):
            pseudo_rounds.append(rnd)
    # 盲投漂移对照: 只给出(讨论终态 vs 盲投终态)配对,语义是否漂移交仲裁人判(不假装机械判等)
    last_by_seat = {}
    for t in ok:
        last_by_seat[t.get("seat")] = t   # rounds 升序遍历,末次覆盖
    bv_by_seat = {b.get("seat"): b for b in (blindvotes or []) if b.get("seat")}
    drift_pairs = []
    for seat, t in last_by_seat.items():
        bv = bv_by_seat.get(seat)
        drift_pairs.append({
            "seat": seat, "role": t.get("role"),
            "discussion_final": t["turn"].get("current_stance"),
            "blind_final": (bv.get("vote") or {}).get("final_stance") if bv else None,
        })
    # 保留分歧: 末态各席 still_holding + 未化解的 rebut
    dissent = []
    for seat, t in last_by_seat.items():
        p = t["turn"]
        rebuts = [r for r in p.get("responses", []) if r.get("stance") == "rebut"]
        dissent.append({"seat": seat, "role": t.get("role"),
                        "still_holding": p.get("still_holding"),
                        "open_rebuttals": [r.get("reason") for r in rebuts]})
    usages = [t.get("usage") for t in ok] + [
        (b.get("usage") if isinstance(b, dict) else None) for b in (blindvotes or [])]
    return {
        "rounds": len(rounds),
        "turns_ok": len(ok),
        "turns_failed": len([t for t in transcript if not t.get("turn")]),
        "participants": sorted({t.get("seat") for t in ok}),
        "conformity_alerts": conformity,
        "conformity_alert": len(conformity) > 0,
        "pseudo_discussion_rounds": pseudo_rounds,
        "early_stop_suggested": bool(pseudo_rounds and pseudo_rounds[-1] == rounds[-1]) if rounds else False,
        "blind_vote_drift_pairs": drift_pairs,
        "dissent_preserved": dissent,
        # 讨论按回合计费(同席多轮各计一次),故用 billed_calls 而非 billed_members(修 C6:
        # review/refine 的 billed_members 是每席一次=席位数;讨论一席多回合,计的是计费调用次数)。
        "token_usage": {**_merge_usage(*usages),
                        "billed_calls": sum(1 for u in usages if u)},
    }


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
        bill = _effective_billing(m)
        # 配置写 subagent 但因 api fallback 实走计费时,显式警示——否则成本被低估
        flag = "  ⚠ 实走 api fallback=计费" if ch == "subagent" and bill == "billed" else ""
        print(f"{m['name']:<18}{m.get('seat','?'):<6}{ch:<10}{m.get('model',''):<28}{m.get('protocol','-')}{flag}")
        if bill == "sub":
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

def resolve_config(path_arg, allow_example_fallback=True):
    """--config 显式给出则用它;否则找 cwd/config.yaml。二者皆无时:
    generate/dry-run 允许回退到 assets/config.example.yaml(首跑体验);
    refine/discuss-* 禁止回退(allow_example_fallback=False)——其语义依赖与生成轮【同一份】
    config,静默换成示例委员会会写出错席位产物、污染 stats(修 P1-2,mem #10096)。"""
    p = Path(path_arg) if path_arg else Path("config.yaml")
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    if not allow_example_fallback:
        sys.exit(f"[config] {p} 不存在,且此阶段(refine/discuss)禁止回退到示例配置——"
                 f"请用 --config 指定与 generate 同一份 config.yaml,否则会用错委员会污染产物。")
    example = SKILL_ROOT / "assets" / "config.example.yaml"
    if example.exists():
        print(f"[hint] {p} not found, using assets/config.example.yaml", file=sys.stderr)
        return yaml.safe_load(example.read_text(encoding="utf-8"))
    sys.exit(f"config not found: {p}. Copy assets/config.example.yaml to config.yaml and edit.")


def validate_config(cfg):
    """最小 schema 校验(修 P1-1 / C3): 缺关键字段给指名报错,而非裸 KeyError/traceback。
    只查会导致运行时崩溃的硬约束,不做全字段校验(配置层刻意宽松,允许自定义键)。"""
    if not isinstance(cfg, dict):
        sys.exit("[config] 顶层必须是 YAML 映射(dict);参照 assets/config.example.yaml")
    members = cfg.get("members")
    if not isinstance(members, list) or not members:
        sys.exit("[config] 缺 members 列表(至少 1 个委员);参照 assets/config.example.yaml")
    names = []
    for i, m in enumerate(members):
        if not isinstance(m, dict) or not m.get("name"):
            sys.exit(f"[config] members[{i}] 缺 name 字段")
        ch = m.get("channel", "api")
        if ch not in ("api", "cli", "subagent"):
            sys.exit(f"[config] members[{i}] ({m.get('name')}) channel={ch!r} 非法(应为 api/cli/subagent)")
        names.append(m["name"])
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        sys.exit(f"[config] member name 重复: {', '.join(dups)}——产物按 name 落盘会互相覆盖,请改唯一名")
    if not isinstance(cfg.get("options"), dict):
        sys.exit("[config] 缺 options 块(max_tokens_member/timeout_seconds/min_successful_members 等);"
                 "参照 assets/config.example.yaml")


# ---------- custom 模式: --members/--models 命令行入口 ----------

_CUSTOM_SEATS = "ABCD"  # 委员上限 4(仲裁人 + ≤4 委员,见 requirements)


def build_custom_members(models_csv: str, members_n=None) -> list:
    """把 --models "id1,id2,..." 构建成 custom 委员会(全 CH3 api 席,座位 A/B/C/D 自动分化角色)。
    重复同一模型 = 主动 Self-MoA;`--members N` + 单模型 = 复制成 N 席 Self-MoA。"""
    models = [m.strip() for m in models_csv.split(",") if m.strip()]
    if not models:
        sys.exit('--models 为空:给逗号分隔的模型 ID,如 --models "openai/gpt-5.6-sol,anthropic/claude-opus-4.8"')
    if members_n is not None:
        if members_n < 1:
            sys.exit(f"--members 需 ≥1,收到 {members_n}")
        if len(models) == 1:
            models = models * members_n            # 单模型 + N 席 = 主动 Self-MoA
        elif len(models) != members_n:
            sys.exit(f"--members {members_n} 与 --models 的 {len(models)} 个模型数不一致;"
                     f"要么省略 --members(席数=模型数),要么给单模型 + --members N(Self-MoA)")
    if len(models) > len(_CUSTOM_SEATS):
        sys.exit(f"custom 委员数上限 {len(_CUSTOM_SEATS)}(座位 {_CUSTOM_SEATS});收到 {len(models)}")
    return [{"name": f"custom-{_CUSTOM_SEATS[i].lower()}", "seat": _CUSTOM_SEATS[i],
             "channel": "api", "protocol": "openrouter", "model": m}
            for i, m in enumerate(models)]


def apply_custom_committee(cfg: dict, args) -> dict:
    """给了 --models 就用它覆盖 cfg['members'](保留 options/custom_roles);否则原样返回。"""
    models_csv = getattr(args, "models", None)
    if not models_csv:
        return cfg
    cfg = dict(cfg)
    cfg["members"] = build_custom_members(models_csv, getattr(args, "members", None))
    return cfg


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
    warn_sensitive_material(material)  # --input 可换文件,精炼轮同样外发,补敏感扫描(C5)
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


def _inject_result(member, mode, parsed) -> dict:
    """把仲裁人外派发的 CH1 子代理 JSON 规约成 _dispatch_channels 同形结果(供 --inject)。"""
    role_key = _seat_role(member, mode)
    return {
        "name": member["name"], "seat": member.get("seat", "?"), "role": role_key,
        "model_used": member.get("model"), "protocol": member.get("protocol", "subagent"),
        "channel_used": "subagent (arbiter-dispatched)",
        "raw": json.dumps(parsed, ensure_ascii=False) if parsed else "",
        "parsed": parsed, "usage": None, "latency_s": 0.0,
        "error": None if parsed else "inject parse failed",
        "err_class": None if parsed else "inject_parse",
    }


def _one_member(cfg, member_filter, what):
    members = _select_members(cfg, member_filter)
    if len(members) != 1:
        sys.exit(f"{what} 需恰好一个 --member(开会讨论按发言序逐席进行);收到 {len(members)}")
    return members[0]


def cmd_discuss_turn(args, cfg):
    """开会讨论单回合(§6 阶段5): 一位委员看见此前发言、发言、追加到 discussion.jsonl。
    CH2/CH3 席由本命令直接派发;CH1 席由仲裁人用 discuss-prompt 取词外派发,再 --inject 回填。"""
    material = Path(args.input).read_text(encoding="utf-8")
    if not args.inject:                # 注入回填不外发;真实派发回合补敏感扫描(C5)
        warn_sensitive_material(material)
    opts = cfg["options"]
    custom_roles = cfg.get("custom_roles", {}) or {}
    m = _one_member(cfg, args.member, "discuss-turn")
    collect = Path(args.collect_dir)
    collect.mkdir(parents=True, exist_ok=True)
    round_no = args.round
    if args.inject:
        parsed = parse_json(Path(args.inject).read_text(encoding="utf-8"))
        if parsed is None:
            sys.exit(f"--inject 内容非合法 JSON: {args.inject}")
        res = _inject_result(m, args.mode, parsed)
    else:
        transcript_str = format_transcript(load_transcript(collect))
        res = run_member_discuss_turn(m, args.mode, material, transcript_str, round_no, opts, custom_roles)
    append_transcript(collect, _turn_envelope(res, round_no))
    status = "OK" if res["parsed"] else f"FAIL[{res['err_class']}]: {res['error']}"
    print(f"[discuss r{round_no}] {m['name']} ({res['role']}) via {res.get('channel_used') or '-'}: {status}",
          file=sys.stderr)


def cmd_discuss_prompt(args, cfg):
    """打印某席本回合(或盲投,--blind)的 system/user 精确 prompt,供仲裁人给 CH1 子代理用同一提示词。"""
    material = Path(args.input).read_text(encoding="utf-8")
    custom_roles = cfg.get("custom_roles", {}) or {}
    m = _one_member(cfg, args.member, "discuss-prompt")
    transcript_str = format_transcript(load_transcript(Path(args.collect_dir)))
    system, user = discuss_prompt(m, args.mode, material, transcript_str, args.round, custom_roles, blind=args.blind)
    print("===SYSTEM===\n" + system + "\n\n===USER===\n" + user)


def cmd_discuss_blindvote(args, cfg):
    """收尾盲投(漂移检测): 委员不看讨论记录,仅凭简报独立复述最终立场。写 blindvote_<seat>.json。"""
    material = Path(args.input).read_text(encoding="utf-8")
    opts = cfg["options"]
    custom_roles = cfg.get("custom_roles", {}) or {}
    m = _one_member(cfg, args.member, "discuss-blindvote")
    collect = Path(args.collect_dir)
    collect.mkdir(parents=True, exist_ok=True)
    if args.inject:
        parsed = parse_json(Path(args.inject).read_text(encoding="utf-8"))
        if parsed is None:
            sys.exit(f"--inject 内容非合法 JSON: {args.inject}")
        res = _inject_result(m, args.mode, parsed)
    else:
        res = run_member_blindvote(m, args.mode, material, opts, custom_roles)
    bv = {"seat": res.get("seat"), "name": res.get("name"), "role": res.get("role"),
          "channel_used": res.get("channel_used"), "model_used": res.get("model_used"),
          "vote": res.get("parsed"), "usage": res.get("usage"),
          "error": res.get("error"), "err_class": res.get("err_class")}
    out = collect / f"blindvote_{res.get('seat')}.json"
    out.write_text(json.dumps(bv, ensure_ascii=False, indent=1), encoding="utf-8")
    status = "OK" if res["parsed"] else f"FAIL[{res['err_class']}]"
    print(f"[blindvote] {m['name']} ({res['role']}): {status} -> {out}", file=sys.stderr)


def cmd_discuss_stats(args, cfg):
    collect = Path(args.collect_dir)
    transcript = load_transcript(collect)
    if not transcript:
        sys.exit(f"no discussion.jsonl in {collect} (先跑 discuss-turn)")
    blindvotes = [json.loads(p.read_text(encoding="utf-8"))
                  for p in sorted(collect.glob("blindvote_*.json"))]
    stats = compute_discuss_stats(transcript, blindvotes)
    out = collect / "discuss_stats.json"
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"\n[discuss-stats] -> {out}", file=sys.stderr)


def cmd_dry_run(args, cfg):
    material = Path(args.input).read_text(encoding="utf-8") if args.input else ""
    dry_run(cfg, args.mode, material, args.topic, args.refine_rounds)


def cmd_leak_check(args):
    """静态自查: 扫描产物/文档/配置有无误落盘的密钥/凭据。命中即非零退出(供 CI/收尾门禁)。
    门禁不变量(修 P0-2): 必须区分「扫过且干净」与「一个文件都没扫到」。默认扫描面全是相对
    路径,从非项目根目录运行时无一存在——旧逻辑会静默打 clean,给出假阴性安全承诺。现先数
    实际可扫文件数,为 0 时以退出码 2 报错(区别于 clean=0 / 命中=1),不再冒充干净。"""
    paths = args.paths or _LEAK_SCAN_DEFAULT
    scanned = sum(1 for root in paths if Path(root).exists()
                  for _ in _iter_text_files(Path(root)))
    if scanned == 0:
        print(f"[leak-check] ✗ 未扫描到任何文件 (paths: {', '.join(paths)}) — 路径不存在或为空。"
              f"请在项目根目录运行,或用 `leak-check <path>...` 显式指定要扫描的路径。", file=sys.stderr)
        sys.exit(2)
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
        _add_custom_flags(p)

    def _add_custom_flags(p):
        p.add_argument("--members", type=int, default=None,
                       help="custom 席位数;单模型 + --members N = 主动 Self-MoA")
        p.add_argument("--models", default=None,
                       help='custom 委员会: 逗号分隔模型 ID(全 CH3),覆盖 config 的 members')

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
    _add_custom_flags(d)
    lc = sub.add_parser("leak-check")
    lc.add_argument("paths", nargs="*",
                    help="要扫描的路径;省略则扫描产物/文档/配置/skill 本体(不含 tests/)")
    # 开会讨论(§6 阶段5): 逐回合、可注入 CH1、盲投、统计
    dt = sub.add_parser("discuss-turn"); common(dt)
    dt.add_argument("--round", type=int, default=1)
    dt.add_argument("--inject", default=None, help="CH1 子代理返回 JSON 的文件路径,回填该席回合")
    dpp = sub.add_parser("discuss-prompt"); common(dpp)
    dpp.add_argument("--round", type=int, default=1)
    dpp.add_argument("--blind", action="store_true", help="打印盲投 prompt 而非讨论回合 prompt")
    dbv = sub.add_parser("discuss-blindvote"); common(dbv)
    dbv.add_argument("--inject", default=None, help="CH1 子代理盲投 JSON 的文件路径")
    dst = sub.add_parser("discuss-stats")
    dst.add_argument("--config", default=None)
    dst.add_argument("--collect-dir", default="moa-reports/run")

    args = ap.parse_args()
    if args.phase == "leak-check":       # 静态自查不需要 config
        cmd_leak_check(args)
        return
    if args.phase in ("stats", "discuss-stats"):  # 只读产物,不需要委员会 config(修 P1-2)
        {"stats": cmd_stats, "discuss-stats": cmd_discuss_stats}[args.phase](args, None)
        return
    # refine/discuss-* 依赖与 generate 同一份 config,禁止静默回退到示例配置(修 P1-2)
    no_fallback = args.phase in ("refine", "discuss-turn", "discuss-prompt", "discuss-blindvote")
    cfg = resolve_config(getattr(args, "config", None), allow_example_fallback=not no_fallback)
    cfg = apply_custom_committee(cfg, args)   # --models 给了就覆盖 members(custom 模式)
    validate_config(cfg)                      # 最小 schema 校验,缺字段指名报错(修 P1-1)
    {"generate": cmd_generate, "refine": cmd_refine,
     "discuss-turn": cmd_discuss_turn, "discuss-prompt": cmd_discuss_prompt,
     "discuss-blindvote": cmd_discuss_blindvote,
     "dry-run": cmd_dry_run}[args.phase](args, cfg)


if __name__ == "__main__":
    main()
