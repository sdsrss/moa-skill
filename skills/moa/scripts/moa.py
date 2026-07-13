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


# 截断重试的预算封顶: 推理模型空壳时倍增 max_tokens,到此为止不再膨胀(防成本失控)。
_MAX_TOKENS_CEILING = 16000


def call_model(cfg: dict, system: str, user: str, temperature: float,
               max_tokens: int, timeout: int, retries: int = 2) -> tuple[str, dict]:
    """瞬态错误指数退避重试;永久错误立即抛出。空响应视为瞬态(Gemini 配额耗尽会静默吞 JSON)。
    截断修复(mem #10216): 推理模型(gemini-3.1-pro/gpt-5.6-sol)在 max_tokens 偏小时 reasoning
    吃光额度,content 空壳且 finish_reason=length——原样重试必然复现,故重试时倍增预算
    (封顶 _MAX_TOKENS_CEILING);末次仍截断但有内容则尽力返回,交上层 parse/修复轮抢救。"""
    url, headers = endpoint_and_headers(cfg)
    last_err = None
    cur_max = max_tokens
    for attempt in range(retries + 1):
        try:
            data = http_post(url, headers, {
                "model": cfg["model"], "max_tokens": cur_max,
                "temperature": temperature,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}, timeout)
            choice = (data.get("choices") or [{}])[0]
            content = choice.get("message", {}).get("content", "") or ""
            finish = choice.get("finish_reason") or choice.get("native_finish_reason") or ""
            if content.strip() and finish != "length":
                return content, (data.get("usage") or {})
            if content.strip() and attempt == retries:   # 加倍后仍截断: 尽力返回部分内容
                return content, (data.get("usage") or {})
            cur_max = min(cur_max * 2, _MAX_TOKENS_CEILING)
            raise TransientError(
                f"empty/truncated response (finish_reason={finish or '-'})",
                err_class="truncated" if finish == "length" else "empty")
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
    """返回 dict 或 None。委员响应 schema 顶层一律是对象——顶层解析出非 dict(数组/标量/
    bool/null)不是有效响应,不能当成功值向下游传递(否则 compute_stats 等对它调 .get() → 崩溃,
    修 ISSUE-001)。顶层非对象时仍尝试从文本里抠出首个 {...} 对象(如模型把响应包成单元素数组
    `[{...}]`),抠不出对象则判 None,交修复轮或计为失败席。"""
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
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


# ---------- CH2b: auggie CLI 通道 (spec: tasks/specs/auggie-cli-channel.md) ----------

def call_cli_auggie(cfg, system, user, timeout):
    """auggie --print 非交互调用(0.32.0 实测,mem #10216):
      - prompt 走 --instruction-file 临时文件,不走 argv(防 ARG_MAX 与注入,对齐 codex stdin)
      - --workspace-root 指向空临时目录: 防索引真实项目、防代码库检索上下文注入盲审
      - --output-format json 取 result 字段(纯文本模式尾部会追加 "Request ID: <uuid>" 污染输出)
      - --max-turns 1 禁 Agent 多轮 / --dont-save-session 不留会话
      - --retry-timeout 限内部限流重试(并发下 Augment 503 内部重试可挂死,实测 >7min);
        subprocess timeout 仍是硬兜底
    计费: Augment 按上游 API 价 +40% 结算,非订阅免费通道(_effective_billing 记 billed)。"""
    auggie_bin = cfg.get("auggie_bin", "auggie")
    if not _which(auggie_bin):
        raise PermanentError(f"{auggie_bin} not found on PATH", err_class="startup",
                             hint="install auggie, or set member.auggie_bin / cli_kind: codex")
    prompt = f"{system}\n\n---\n\n{user}\n\n只输出 JSON,不要任何其他文字。"
    with tempfile.TemporaryDirectory(prefix="moa_auggie_") as td:
        ws = Path(td) / "ws"
        ws.mkdir()
        pf = Path(td) / "prompt.txt"
        pf.write_text(prompt, encoding="utf-8")
        cmd = [auggie_bin, "--print", "--quiet", "--output-format", "json",
               "--max-turns", "1", "--dont-save-session",
               "--retry-timeout", str(max(30, timeout // 3)),
               "--workspace-root", str(ws), "--instruction-file", str(pf)]
        if cfg.get("model"):
            cmd += ["--model", cfg["model"]]
        cmd += list(cfg.get("cli_extra", []) or [])
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TransientError(f"auggie timeout after {timeout}s", err_class="timeout")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace")[:200]
            ec = "auth" if re.search(r"login|auth|credential|401|403", err, re.I) else "cli"
            raise (PermanentError if ec == "auth" else TransientError)(
                f"auggie exit {proc.returncode}: {err}", err_class=ec)
        out = proc.stdout.decode("utf-8", "replace")
        try:
            env = json.loads(out)
            if env.get("is_error"):
                raise TransientError(f"auggie is_error: {str(env.get('result'))[:200]}",
                                     err_class="cli")
            text = (env.get("result") or "").strip()
        except json.JSONDecodeError:  # 信封解析失败(版本差异): 回退纯文本,剥 Request ID 尾行
            text = re.sub(r"\n+Request ID: [0-9a-fA-F-]+\s*$", "", out).strip()
        if not text:
            raise TransientError("auggie empty output shell", err_class="empty")
        return text, parse_json(text)


# ---------- 通道调度 (fallback 链: api / cli 混合;subagent 脚本外) ----------

_auggie_announced = False


def _expand_cli(cfg, note):
    """cli 席按 cli_kind 展开成尝试列表。auto(默认): 检测到 auggie 二进制则优先
    (稳定、一个账号模型全,2026-07-13 用户指令),codex 殿后;auto 的 auggie try
    不继承 cli_extra(codex 专属 flag 如 -c,在 auggie 里语义完全不同),模型只取
    auggie_model(两侧模型 ID 命名空间不同: gpt5.6-sol vs openai/gpt-5.6-sol)。
    显式 cli_kind: codex/auggie = 完全按成员配置跑(codex 即 auto 的 opt-out 路径)。"""
    global _auggie_announced
    kind = cfg.get("cli_kind", "auto")
    if kind in ("codex", "auggie"):
        return [("cli", {**cfg, "cli_kind": kind}, note)]
    tries = []
    if _which(cfg.get("auggie_bin", "auggie")):
        if not _auggie_announced:
            print("[cli] auggie detected → preferred for channel:cli seats "
                  "(set member.cli_kind: codex to opt out)", file=sys.stderr)
            _auggie_announced = True
        tries.append(("cli", {**cfg, "cli_kind": "auggie",
                              "model": cfg.get("auggie_model"), "cli_extra": []},
                      (note + "; " if note else "") + "auto→auggie"))
    if _which(cfg.get("codex_bin", "codex")):
        tries.append(("cli", {**cfg, "cli_kind": "codex"},
                      (note + "; " if note else "") + "auto→codex"))
    if not tries:  # 两个二进制都不在: 仍给 codex try,让 startup 错误浮出而非静默 0 通道
        tries.append(("cli", {**cfg, "cli_kind": "codex"}, note))
    return tries


def resolve_channel(member: dict):
    """返回按 fallback 链展开的 (kind, cfg, note) 尝试列表。
    channel=api/cli 直接可跑;channel=subagent 由仲裁人脚本外派发,此处跳过(仅收其 fallback)。
    cli 席再按 cli_kind(auto/codex/auggie)展开,见 _expand_cli。"""
    tries = []
    ch = member.get("channel", "api")
    if ch == "api":
        tries.append(("api", member, ""))
    elif ch == "cli":
        tries += _expand_cli(member, "")
    for fb in member.get("fallback", []) or []:
        fch = fb.get("channel", "api")
        merged = {**member, **fb}
        note = f"fallback from channel={ch}"
        if fch == "api":
            tries.append(("api", merged, note))
        elif fch == "cli":
            tries += _expand_cli(merged, note)
    return tries


def _effective_billing(member) -> str:
    """dry-run 计费判定:返回 'billed'(计费) 或 'sub'(订阅/免费)。
    按 moa.py *真正会跑* 的通道判定,而非配置的主通道——纯 subagent(无 api/cli fallback)由仲裁人
    免费派发(sub);否则看 resolve_channel 首个 try: cli:codex=订阅(sub),cli:auggie=Augment 按
    上游价+40% 计费(billed),api=计费(billed)。修正旧逻辑只看主通道、把 'subagent + api fallback'
    误记为免费的少报 bug;auggie 若归订阅同样会低估成本。"""
    tries = resolve_channel(member)
    if not tries:
        return "sub"                        # 纯 subagent → 仲裁人免费派发
    kind, ccfg, _ = tries[0]
    if kind == "cli":
        return "billed" if ccfg.get("cli_kind") == "auggie" else "sub"
    return "billed"


def _fallback_has_billed(member) -> bool:
    """member 的展开尝试链里是否含计费通道(api 或 cli:auggie)。供 dry-run 提示"首选订阅但
    降级会转计费"(修 F6)——判的是整条链,与 _effective_billing 只判首 try 的口径互补。"""
    return any((k == "api") or (k == "cli" and c.get("cli_kind") == "auggie")
               for k, c, _ in resolve_channel(member))


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
                ckind = ccfg.get("cli_kind", "codex")
                fn = call_cli_auggie if ckind == "auggie" else call_cli_codex
                raw, parsed = fn(ccfg, system, user, timeout)
                usage = None  # cli 通道无 usage 折算(codex 走订阅;auggie 在 Augment 侧结算)
                if parsed is None:  # 解析失败先给一次 CLI 修复轮(对齐 api 路径),仍失败才交 fallback
                    _, parsed = _cli_json_repair(fn, ccfg, raw, timeout)
                    if parsed is None:
                        raise TransientError(f"{ckind} output not valid JSON after repair",
                                             err_class="parse")
            else:
                raw, parsed, usage = call_with_json_repair(
                    ccfg, system, user, member.get("temperature_generate", default_temp),
                    opts["max_tokens_member"], timeout, None)
            label = f"cli:{ccfg.get('cli_kind', 'codex')}" if kind == "cli" else kind
            return {
                "name": member["name"], "seat": seat, "role": role_key,
                "model_used": ccfg.get("model"),  # codex 席可省 model(用 codex 默认)→ None,非 KeyError
                "protocol": ccfg.get("protocol", "-" if kind == "cli" else "openrouter"),
                "channel_used": label + (f" ({note})" if note else ""),
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


def _cli_json_repair(fn, ccfg, raw, timeout):
    """cli 路径的一次性 JSON 自修复(与 api 路径 call_with_json_repair 对齐;实测 5 跑 2 跑
    模型在 JSON 字符串值里写未转义引号,直接把该席丢给 fallback 太浪费——修复轮多为轻推理)。"""
    return fn(ccfg,
              "你上一次的输出不是合法 JSON。把其中的实质内容原样转成合法 JSON,不要增删观点,不要解释。",
              f"你上一次的输出:\n{raw}", timeout)


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
    visible = [t for t in turns if isinstance(t.get("turn"), dict)]  # 非对象回合不入记录(ISSUE-002)
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
                             for r in _dict_items(p.get("responses")))
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
    """读 discussion.jsonl(每行一个回合信封);不存在则空。
    损坏行(中断写入 / 手工误编辑)跳过并到 stderr 计数告警,不让整场讨论因一行崩溃(修 N3)——
    与 leak_check 对不可读文件的容错风格一致:宁可少一回合,不要 discuss-stats 直接 traceback。"""
    p = Path(collect_dir) / "discussion.jsonl"
    if not p.exists():
        return []
    out, bad = [], 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    if bad:
        print(f"[discuss] warning: skipped {bad} corrupt line(s) in {p}", file=sys.stderr)
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
    宽限;超时者标 skipped_grace(不算失败)。每完成一个即回调 on_done(res) 落盘,
    保证即便落伍者拖尾,collect-dir 也已有法定结果。返回按 members 原序的结果列表。

    按席宽限(v1.6.0): 每个落伍席的窗长 = member.get("grace_seconds", grace_s)——即
    可给"高价值但慢"的旗舰席(如重推理模型)单独放宽,不被全局小窗牺牲,而其余席仍用
    全局默认。故不再是【单一全局 deadline 一刀切】,而是达法定数时给当时每个 pending 席各记
    自己的到期时刻,逐席独立到期、独立 skip。未设 member 级值的席行为与旧版一致(向后兼容)。

    止损语义(修 P0-1): 宽限到期必须让【本函数立即返回】,不得 join 落伍线程。此前用
    `with ThreadPoolExecutor` 管理,块退出隐式 shutdown(wait=True) 会 join 全部线程,
    使本函数阻塞至最慢席结束(实测 3 席 grace=0.5s 函数仍 6s 才返回)。现改手动管理:
    任一席被弃即 shutdown(wait=False),函数即刻返回,仲裁流程拿到法定结果继续、collect-dir
    已有法定产物。范围限定: 界定的是【函数返回延迟】,不是进程总 wall-clock——落伍工作线程
    仍在后台跑,concurrent.futures 的 atexit 会在解释器退出时 join 它们,故 `generate`
    进程收尾可能再等落伍席一小段(上界 = member 级 timeout,不无限拖尾)。要连进程退出也
    界定需改 daemon 线程,但那会硬杀在途 HTTP,得不偿失,故不做。"""
    results = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(members)))
    abandoned = False
    try:
        futs = {ex.submit(fn, m): m for m in members}
        pending = set(futs)
        ok = 0
        deadlines = {}   # fut -> 各席 monotonic 到期时刻; 达法定数后一次性登记, 之后逐席到期
        while pending:
            timeout = None
            live = [deadlines[f] for f in pending if f in deadlines]
            if live:
                timeout = max(0.0, min(live) - time.monotonic())
            done, pending = concurrent.futures.wait(
                pending, timeout=timeout, return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                m = futs[fut]
                r = fut.result()
                results[m["name"]] = r
                if on_done:
                    on_done(r)
                if r.get("parsed"):
                    ok += 1
            # 达法定数: 给当时每个 pending 席登记各自窗(member 级覆盖全局), 只登记一次。
            if not deadlines and ok >= quorum_target and pending:
                now = time.monotonic()
                for fut in pending:
                    g = futs[fut].get("grace_seconds")
                    deadlines[fut] = now + (grace_s if g is None else g)
            # 逐席检查: 已过自身窗的落伍席即弃(不等其余席), 立即返回(不 join)。
            if deadlines:
                now = time.monotonic()
                for fut in [f for f in pending if f in deadlines and now >= deadlines[f]]:
                    m = futs[fut]
                    r = _skipped_grace(m)
                    results[m["name"]] = r
                    if on_done:
                        on_done(r)
                    fut.cancel()  # 尚未起跑的能真取消; 已在跑的由 member 级 timeout 自行了结
                    pending.discard(fut)
                    abandoned = True
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


def _parsed_ok(r) -> bool:
    """成功席判据: parsed 必须是【对象】。合法但非 dict 的 JSON(数组/标量/bool)不是有效委员
    响应,不能计入共识——否则下游对它调 .get() 会崩溃(修 ISSUE-001)。moa.py 生成的 parsed 经
    parse_json 已保证 dict-or-None;但 CH1 席由仲裁人脚本外手写 member_*.json、或历史/误编辑产物
    可能带非对象 parsed,聚合层须自证健壮,不靠上游。"""
    return isinstance(r.get("parsed"), dict)


def _dict_items(v) -> list:
    """把 schema 里「对象数组」字段(issues/opponent_fatal_flaws/ideas/cross_exam/
    verdicts_on_others/responses)收敛成仅含 dict 的列表(修 ISSUE-002): 容忍模型把字段写成
    字符串/标量,或在对象数组里混入裸字符串。非 list 一律视为空;list 内非 dict 元素剔除——
    单席畸形字段不得让整个 stats/discuss 聚合崩栈,其余席已付费产物照常聚合。"""
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]


def _num(v) -> float:
    """把 confidence 之类数值字段收敛成 float(修 ISSUE-002): 模型偶尔把它写成字符串
    ('high' / '0.8')或 null,直接进 sum() 会 TypeError。数字型(排除 bool)原样;
    可转的数字字符串尽力转;其余记 0.0(与'缺失'同权,不夸大也不崩)。"""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return 0.0
    return 0.0


def _str(v) -> str:
    """把该是字符串的字段收敛成 str(修 ISSUE-002): 模型偶尔把它写成 list/数字/null,
    非字符串一律记空串,供 .strip()/拼接安全使用。"""
    return v if isinstance(v, str) else ""


def compute_stats(mode: str, results: list) -> dict:
    ok = [r for r in results if _parsed_ok(r)]
    failed = [r for r in results if not _parsed_ok(r)]
    base = {
        "degraded": len(failed) > 0,
        "members_ok": len(ok),
        "members_failed": len(failed),
        "roster": [{"name": r["name"], "seat": r.get("seat"),
                    "model_used": r.get("model_used"), "channel_used": r.get("channel_used"),
                    "ok": _parsed_ok(r)} for r in results],
        "failures": [{"name": r["name"], "err_class": r.get("err_class"), "error": r.get("error")}
                     for r in failed],
        "token_usage": _aggregate_usage(ok),
    }
    if mode == "review":
        sev = {"blocker": 0, "high": 0, "medium": 0, "low": 0}
        verdicts, confs = {}, []
        for r in ok:
            p = r["parsed"]
            v = p.get("verdict") if isinstance(p.get("verdict"), str) else "?"
            verdicts[v] = verdicts.get(v, 0) + 1
            confs.append(_num(p.get("confidence")))
            for i in _dict_items(p.get("issues")):
                s = i.get("severity") if isinstance(i.get("severity"), str) else "low"
                sev[s] = sev.get(s, 0) + 1
        base.update(verdict_tally=verdicts, issue_count_by_severity=sev,
                    mean_confidence=round(sum(confs) / len(confs), 2) if confs else None)
    elif mode == "decide":
        claims, confs = {}, []
        flaws = {"fatal": 0, "major": 0, "minor": 0}
        spikes = 0
        for r in ok:
            p = r["parsed"]
            c = p.get("claimed_option") if isinstance(p.get("claimed_option"), str) else "?"
            claims[c] = claims.get(c, 0) + 1
            confs.append(_num(p.get("confidence")))
            for f in _dict_items(p.get("opponent_fatal_flaws")):
                s = f.get("severity") if isinstance(f.get("severity"), str) else "minor"
                flaws[s] = flaws.get(s, 0) + 1
            sp = p.get("spike_suggestion")
            if isinstance(sp, str) and sp.strip():
                spikes += 1
        base.update(option_claims=claims, flaw_count_by_severity=flaws,
                    spike_suggestions=spikes,
                    mean_confidence=round(sum(confs) / len(confs), 2) if confs else None)
    else:  # brainstorm
        total = sum(len(_dict_items(r["parsed"].get("ideas"))) for r in ok)
        solos = sum(1 for r in ok for i in _dict_items(r["parsed"].get("ideas"))
                    if _num(i.get("novelty")) >= 4)
        base.update(total_ideas_before_dedup=total, high_novelty_ideas=solos)
    return base


ANON_LABELS = "甲乙丙丁戊己庚辛"


def anonymize_others(all_results, exclude_name):
    """把除 exclude_name 外的成功委员意见匿名化(标签甲乙丙…,去掉 name/model,防大牌压人)。"""
    out = []
    i = 0
    for r in all_results:
        if r["name"] == exclude_name or not _parsed_ok(r):  # 非对象意见不喂给互评轮(ISSUE-001)
            continue
        out.append({"评审员": ANON_LABELS[i % len(ANON_LABELS)], "意见": r["parsed"]})
        i += 1
    return out


def _majority_verdict(results, field):
    tally = {}
    for r in results:
        if _parsed_ok(r):
            v = r["parsed"].get(field)
            if v is not None:
                tally[v] = tally.get(v, 0) + 1
    if not tally:
        return None
    # 平票无多数派(修 F4): 最高票并列时返回 None,不让 dict 插入序决定"多数派"——
    # 否则 sycophancy 基准会把翻向"伪多数"误计为翻向多数(2:2 时 max() 取先插入的键)。
    top = max(tally.values())
    leaders = [k for k, c in tally.items() if c == top]
    return leaders[0] if len(leaders) == 1 else None


def compute_refine_stats(mode: str, prior_results: list, refine_results: list) -> dict:
    """精炼轮统计(design.md §7.3): 三态计票、一票 challenge 锁 disputed、谄媚计数器、早停信号。
    prior_results = 上一轮(生成或前一精炼轮)产物;refine_results = 本精炼轮产物。"""
    ok = [r for r in refine_results if _parsed_ok(r)]
    base: dict = {
        "round_members_ok": len(ok),
        "round_members_failed": len(refine_results) - len(ok),
        "token_usage": _aggregate_usage(ok),  # 本精炼轮计费席 token 增量,供成本增量观测
    }
    if mode == "review":
        stance = {"validate": 0, "challenge": 0, "abstain": 0}
        challenged_titles = {}
        for r in ok:
            for v in _dict_items(r["parsed"].get("verdicts_on_others")):
                st = v.get("stance") if isinstance(v.get("stance"), str) else "abstain"
                stance[st] = stance.get(st, 0) + 1
                if st == "challenge":
                    rt = v.get("ref_title")
                    t = rt.strip() if isinstance(rt, str) else ""
                    if t:
                        challenged_titles[t] = challenged_titles.get(t, 0) + 1
        # 谄媚计数器: 相对上一轮,verdict 向上一轮多数派翻转、且本轮该员未提出任何 challenge(无新证据代理)
        prior_by = {r["name"]: r for r in prior_results if _parsed_ok(r)}
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
                                     for v in _dict_items(r["parsed"].get("verdicts_on_others")))
                # majority is not None 守卫(配合 F4 平票→None): 无多数派时不存在"翻向多数",
                # 且防 new_v 亦为 None(verdict 缺失)时 None==None 误计一次 flip。
                if majority is not None and new_v == majority and not made_challenge:
                    flips_toward_majority += 1
        sycophancy_alert = movers > 0 and (flips_toward_majority / movers) > 0.5
        # 早停信号: 本轮 verdict 全一致 且 无 disputed 且 无席位失败(修 F3)——
        # 有席位本轮失败则证据不全,失败席立场缺席,"全一致"可能是幸存者偏差,不建议早停。
        cur_verdicts = {r["parsed"].get("verdict") for r in ok}
        early_stop = (len(cur_verdicts) == 1 and not challenged_titles
                      and base["round_members_failed"] == 0)
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
        prior_by = {r["name"]: r for r in prior_results if _parsed_ok(r)}
        for r in ok:
            for e in _dict_items(r["parsed"].get("cross_exam")):
                s = e.get("attack_severity") if isinstance(e.get("attack_severity"), str) else "minor"
                exam[s] = exam.get(s, 0) + 1
            pj = prior_by.get(r["name"])
            if pj and pj["parsed"].get("claimed_option") != r["parsed"].get("revised_claimed_option"):
                shifts += 1
        cur_opts = {r["parsed"].get("revised_claimed_option") for r in ok}
        base.update(cross_exam_by_severity=exam, option_shifts=shifts,
                    early_stop_suggested=(len(cur_opts) == 1     # 修 F3: 同 review,有失败席不早停
                                          and base["round_members_failed"] == 0))
    return base


def compute_discuss_stats(transcript: list, blindvotes: list) -> dict:
    """开会讨论统计: 从众计数(无新论据却改立场) + 假讨论(整轮无新论据) + 盲投漂移对照 + 保留分歧。
    transcript = discussion.jsonl 全部回合;blindvotes = blindvote_<seat>.json 的 parsed 列表(可空)。"""
    ok = [t for t in transcript if isinstance(t.get("turn"), dict)]  # turn 须为对象,否则下游 .get() 崩溃(ISSUE-001)
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
        if turns and all(not _str(x.get("new_argument")).strip() for x in turns):
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
        rebuts = [r for r in _dict_items(p.get("responses")) if r.get("stance") == "rebut"]
        dissent.append({"seat": seat, "role": t.get("role"),
                        "still_holding": p.get("still_holding"),
                        "open_rebuttals": [r.get("reason") for r in rebuts]})
    usages = [t.get("usage") for t in ok] + [
        (b.get("usage") if isinstance(b, dict) else None) for b in (blindvotes or [])]
    return {
        "rounds": len(rounds),
        "turns_ok": len(ok),
        "turns_failed": len([t for t in transcript if not isinstance(t.get("turn"), dict)]),
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
        # 配置写 subagent 但因 fallback 实走计费(api 或 auggie)时,显式警示——否则成本被低估
        if ch == "subagent" and bill == "billed":
            flag = "  ⚠ 实走计费 fallback"
        # 首选订阅通道(codex)但 fallback 链里含计费通道时提示: 首选失败降级即转计费(修 F6)。
        # _effective_billing 只判首 try(最可能路径,口径不变),此处补链上计费面的可见性。
        elif bill == "sub" and _fallback_has_billed(m):
            flag = "  ⚠ fallback 含计费通道,降级时转计费"
        else:
            flag = ""
        print(f"{m['name']:<18}{m.get('seat','?'):<6}{ch:<10}{m.get('model',''):<28}{m.get('protocol','-')}{flag}")
        if bill == "sub":
            api_calls_sub += 1
        else:
            api_calls_billed += 1
    total_calls_each = (1 + refine_rounds)
    print(f"\n外部委员调用数 = {n} 席 × {total_calls_each}(生成+精炼) = {n * total_calls_each}")
    print(f"  其中计费通道(CH3 api / CH2 auggie=上游价+40%): {api_calls_billed} 席 × {total_calls_each} = {api_calls_billed * total_calls_each} 次")
    print(f"  订阅通道(CH1 subagent / CH2 codex): {api_calls_sub} 席 × {total_calls_each} = {api_calls_sub * total_calls_each} 次(只计次数,不折算美元)")
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
    def _bad_grace(v):
        # grace_seconds(全局/按席)进 dispatch_with_quorum 做 `now + v` 算术: 非数值(如 YAML 引号
        # 化的 "150")会裸 TypeError 中止整轮; 负值(如手误 -5)使窗立即过期→本想"保住慢席"却静默
        # 反把它秒弃。未设(None)= 用默认, 合法。bool 是 int 子类但语义非秒数, 一并拒。
        return v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0)
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
        ck = m.get("cli_kind", "auto")
        if ck not in ("auto", "codex", "auggie"):
            sys.exit(f"[config] members[{i}] ({m.get('name')}) cli_kind={ck!r} 非法(应为 auto/codex/auggie)")
        # 告警(不阻断,修 F5): channel=cli + auto(默认)+ 设了 model 但无 auggie_model 时,
        # 检测到 auggie 会优先走 auggie 且只认 auggie_model(两侧 ID 命名空间不同),member.model 被
        # 静默忽略、auggie 用其默认模型顶替——委员构成偏离配置意图且 model_used 记 None。指名提示。
        if ch == "cli" and ck == "auto" and m.get("model") and not m.get("auggie_model"):
            print(f"[config] ⚠ members[{i}] ({m.get('name')}) channel=cli 未显式 cli_kind(=auto),"
                  f"检测到 auggie 时优先走 auggie 且只认 auggie_model;你设了 model={m['model']!r} 但无 "
                  f"auggie_model,auggie 路径会用其默认模型顶替。要精确控制请显式 cli_kind 或补 auggie_model。",
                  file=sys.stderr)
        if _bad_grace(m.get("grace_seconds")):
            sys.exit(f"[config] members[{i}] ({m.get('name')}) grace_seconds="
                     f"{m.get('grace_seconds')!r} 非法(应为非负数值秒数,如 150 或 90.0)")
        names.append(m["name"])
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        sys.exit(f"[config] member name 重复: {', '.join(dups)}——产物按 name 落盘会互相覆盖,请改唯一名")
    # 文件名规范化(_safe_name 把 / : 空格等映射为 _)后的碰撞也会互相覆盖:原始名唯一 ≠ 落盘名唯一。
    # 否则两个相异合法名(如 'a/b' 与 'a_b')同写 member_a_b.json,后者覆盖前者→该席静默从仲裁/stats 消失。
    safe_seen = {}
    for n in names:
        s = _safe_name(n)
        if s in safe_seen:
            sys.exit(f"[config] member name {n!r} 与 {safe_seen[s]!r} 经文件名规范化后同为 "
                     f"member_{s}.json,会互相覆盖;请改用规范化后仍相异的名字")
        safe_seen[s] = n
    opts = cfg.get("options")
    if not isinstance(opts, dict):
        sys.exit("[config] 缺 options 块(max_tokens_member/timeout_seconds/min_successful_members 等);"
                 "参照 assets/config.example.yaml")
    if _bad_grace(opts.get("grace_seconds")):
        sys.exit(f"[config] options.grace_seconds={opts.get('grace_seconds')!r} "
                 f"非法(应为非负数值秒数,如 90)")


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

    # 全 CH1 配置: moa.py 无可派发席,整会交仲裁人脚本外派发。这不是"顾问不足",干净退出即可
    # (修 N1: 旧代码此处落到下方 min_ok 门,以 abort 收场,措辞误导仲裁人放弃本可成立的委员会)。
    if not dispatchable:
        print("[generate] no api/cli members to dispatch; all seats are channel=subagent "
              "(arbiter-dispatched). Nothing for moa.py to run — 交由仲裁人 Task 外派发。",
              file=sys.stderr)
        return
    print(f"[generate] dispatching {len(dispatchable)} members ({args.mode}) ...", file=sys.stderr)
    # 法定数(min_ok)只对【本脚本可派发席】负责,故分母用 len(dispatchable) 而非 len(members)
    # (修 N1: 旧代码分母含纯 subagent 席,但 ok 只统计脚本派发结果——默认 config 恰是 2 subagent +
    # 2 可派发,可派发席掉一个就 ok=1<min_ok=2 被误 abort,废掉 fallback/quorum 想保的降级续跑。
    # 纯 subagent 席由仲裁人脚本外派发,合流后含 CH1 的整体法定数由仲裁人在 collect-dir 上判)。
    min_ok = min(opts.get("min_successful_members", 2), max(1, len(dispatchable)))
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
    # subagent 席位可能由仲裁人另行写入,此门只判本脚本可派发席(min_ok 分母亦为 dispatchable,见上)
    if len(ok) < min_ok:
        sys.exit(f"[abort] dispatchable members {len(ok)} ok < required {min_ok} — "
                 f"顾问不足的结论不配称为委员会评审(纯 subagent 席由仲裁人另行派发,不计入此数)。")
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

    min_ok = min(opts.get("min_successful_members", 2), max(1, len(dispatchable)))  # 同 N1: 分母用 dispatchable
    quorum_target = max(min_ok, len(dispatchable) - 1)
    results = dispatch_with_quorum(
        dispatchable, one, quorum_target, opts.get("grace_seconds", 30), on_done=_log_write)
    ok = [r for r in results if r["parsed"]]
    # 止损门(修 F2): 有可派发席却全数精炼失败 → 本轮无产出,非零退出,与 cmd_generate 的 abort 对齐。
    # (失败席保留上轮意见是设计——但"全员失败"意味整轮零信息增量,静默 exit 0 会让脚本化串命令
    #  误以为精炼发生过。dispatchable 为空=全 CH1 配置,由仲裁人外派发,不在此门内。)
    if dispatchable and not ok:
        sys.exit(f"[refine r{round_no}] abort: 0/{len(dispatchable)} 可派发席产出精炼意见 —— "
                 f"本轮精炼无产出(失败席沿用上轮意见,但整轮零信息增量)。排查上游错误后重试,"
                 f"或直接用上一轮产物收敛。")
    degraded = len(ok) < len(dispatchable)
    tag = " [DEGRADED: 部分席位精炼失败,保留其上轮意见]" if degraded else ""
    print(f"[refine r{round_no}] done: {len(ok)}/{len(dispatchable)} ok{tag} -> {collect}", file=sys.stderr)


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
    out = collect / f"blindvote_{_safe_name(str(res.get('seat')))}.json"  # 修 N4: seat 也过路径穿越门
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
