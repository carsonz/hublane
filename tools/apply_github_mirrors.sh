#!/usr/bin/env bash
# 把 github.com 镜像链应用到已安装的 hublane 服务(需要 root)。
#
# 背景: github.com 原先只有 direct / watt 两个上游。direct 会被链路在正好
# 128 KiB 处掐断(SSLEOFError, 传输中断于 131072 字节), 导致 git 的 pack 下不来,
# clone/fetch 卡死或 502。本脚本把镜像补到 github_upstreams 最前面。
#
# 用法: sudo bash tools/apply_github_mirrors.sh
# 回滚: 见脚本打印的备份路径。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="/opt/hublane"
TS="$(date +%Y%m%d-%H%M%S)"
CHAIN='["ghproxy_com_gh", "ghfast_gh", "ghproxy_net_gh", "direct", "watt"]'

[ "$(id -u)" = "0" ] || { echo "需要 root: sudo bash $0" >&2; exit 1; }
[ -d "$DEST" ] || { echo "未找到 $DEST, 请先 sudo bash install.sh" >&2; exit 1; }

echo "==> 备份"
cp -v "$DEST/config.json"  "$DEST/config.json.bak-$TS"
cp -v "$DEST/hublane.py"   "$DEST/hublane.py.bak-$TS"

echo "==> 部署 hublane.py"
install -m 0755 "$REPO/hublane.py" "$DEST/hublane.py"

echo "==> 设置 github_upstreams = $CHAIN"
python3 - "$DEST/config.json" "$CHAIN" <<'PY'
import json, sys
path, chain = sys.argv[1], json.loads(sys.argv[2])
cfg = json.load(open(path))
cfg["github_upstreams"] = chain
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("   github_upstreams =", cfg["github_upstreams"])
PY

echo "==> 校验配置"
"$DEST/hublane.py" --config "$DEST/config.json" --check

echo "==> 重启服务"
if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
    systemctl restart hublane
    sleep 2
    systemctl is-active hublane
else
    pkill -f "$DEST/hublane.py" 2>/dev/null || true
    nohup "$(command -v python3)" "$DEST/hublane.py" --config "$DEST/config.json" \
        >/tmp/hublane.log 2>&1 &
    sleep 3
    echo "   已以 nohup 方式启动"
fi

echo
echo "完成。验证:  cd 任意仓库 && git fetch origin"
echo "回滚:       sudo cp $DEST/config.json.bak-$TS $DEST/config.json"
echo "            sudo cp $DEST/hublane.py.bak-$TS  $DEST/hublane.py && sudo systemctl restart hublane"