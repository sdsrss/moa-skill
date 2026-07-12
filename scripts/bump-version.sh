#!/usr/bin/env bash
# 版本号单一来源同步器(修 P1-7 子项:版本号散在 4 处手改会漂移,审计已抓到一次 v1.1.2 vs 1.1.5)。
#
# 唯一源 = .claude-plugin/plugin.json 的 "version"。其余从它同步:
#   - skills/moa/SKILL.md 的 "实现状态:vX.Y.Z" banner
#   - README.md / README.zh-CN.md 的 status-vX.Y.Z 徽章
#   - .claude-plugin/marketplace.json 的 metadata.version 与 plugins[].version(两处均 = 插件版本)
#
# 用法:
#   scripts/bump-version.sh <x.y.z>   将版本号写到全部位置
#   scripts/bump-version.sh --check   断言全部一致(漂移则非零退出;供发版前门禁/CI)
set -euo pipefail
cd "$(dirname "$0")/.."

PLUGIN=.claude-plugin/plugin.json
MARKET=.claude-plugin/marketplace.json
SKILL=skills/moa/SKILL.md
READMES=(README.md README.zh-CN.md)

cur=$(grep -oE '"version"[[:space:]]*:[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"' "$PLUGIN" \
      | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
[ -n "$cur" ] || { echo "[bump] 无法从 $PLUGIN 读出 version" >&2; exit 2; }

check() {
  local fail=0
  grep -q "实现状态:v$cur" "$SKILL" || { echo "[drift] $SKILL banner ≠ $cur" >&2; fail=1; }
  for r in "${READMES[@]}"; do
    grep -q "status-v$cur" "$r" || { echo "[drift] $r 徽章 ≠ $cur" >&2; fail=1; }
  done
  if [ -f "$MARKET" ]; then
    # 按【字段】精确校验(grep 计数拦不住"二缺一":删掉 metadata.version、plugin 那行仍匹配也会漏判)。
    # 要求 metadata.version 与每个 plugins[].version 都存在且 = cur。python3 本就是硬依赖。
    python3 - "$MARKET" "$cur" <<'PY' || fail=1
import json, sys
m = json.load(open(sys.argv[1])); v = sys.argv[2]; bad = []
if m.get("metadata", {}).get("version") != v:
    bad.append(f"metadata.version={m.get('metadata', {}).get('version')!r}")
for i, p in enumerate(m.get("plugins", [])):
    if p.get("version") != v:
        bad.append(f"plugins[{i}].version={p.get('version')!r}")
if bad:
    sys.stderr.write(f"[drift] marketplace.json 应全部 = {v}: {', '.join(bad)}\n"); sys.exit(1)
PY
  fi
  if [ "$fail" -eq 0 ]; then
    echo "[version] 全部一致 @ $cur ($PLUGIN, $MARKET, $SKILL, ${READMES[*]})"
  else
    echo "[version] 版本号漂移,请跑 scripts/bump-version.sh $cur 同步" >&2
    exit 1
  fi
}

bump() {
  local new="$1"
  [[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "[bump] 版本号格式非法: $new (应为 x.y.z)" >&2; exit 2; }
  sed -i -E "s/(\"version\"[[:space:]]*:[[:space:]]*\")[0-9]+\.[0-9]+\.[0-9]+(\")/\1$new\2/" "$PLUGIN"
  [ -f "$MARKET" ] && sed -i -E "s/(\"version\"[[:space:]]*:[[:space:]]*\")[0-9]+\.[0-9]+\.[0-9]+(\")/\1$new\2/g" "$MARKET"
  sed -i -E "s/(实现状态:v)[0-9]+\.[0-9]+\.[0-9]+/\1$new/" "$SKILL"
  sed -i -E "s/(status-v)[0-9]+\.[0-9]+\.[0-9]+/\1$new/g" "${READMES[@]}"
  echo "[version] $cur -> $new  ($PLUGIN, $MARKET, $SKILL, ${READMES[*]})"
}

case "${1:-}" in
  --check) check ;;
  "")      echo "用法: $0 <x.y.z> | --check" >&2; exit 2 ;;
  *)       bump "$1" ;;
esac
