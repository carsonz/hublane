#!/usr/bin/env bash
# hublane : WSL/Linux 卸载脚本 (install.sh 的逆操作)
#
#   sudo bash uninstall.sh           # 停服务 + 撤 CA + 清 shell 变量, 保留 /opt/hublane
#   sudo bash uninstall.sh --purge   # 连同 /opt/hublane (含本地 CA 私钥) 一起删除
set -uo pipefail

DEST="/opt/hublane"
CA_DEB="/usr/local/share/ca-certificates/hublane-local-ca.crt"
CA_RPM="/etc/pki/ca-trust/source/anchors/hublane-local-ca.crt"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$USER_NAME" 2>/dev/null | cut -d: -f6)"
[ -n "$USER_HOME" ] || USER_HOME="$HOME"

if [ "$(id -u)" != "0" ]; then
  echo "需要 root: sudo bash uninstall.sh [--purge]" >&2
  exit 1
fi

echo "==> [1/5] 停止 systemd 服务"
if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now hublane >/dev/null 2>&1 && echo "    已停止并禁用 hublane.service" \
    || echo "    systemd 服务不存在或已停止"
else
  echo "    无 systemd, 跳过"
fi
rm -f /etc/systemd/system/hublane.service
command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload 2>/dev/null

echo "==> [2/5] 结束残留进程"
pkill -f 'hublane\.py' 2>/dev/null && echo "    已结束 nohup 直启的进程" || true

echo "==> [3/5] 从系统信任库移除本地 CA"
removed=0
if [ -f "$CA_DEB" ]; then
  rm -f "$CA_DEB"
  command -v update-ca-certificates >/dev/null 2>&1 && update-ca-certificates --fresh 2>&1 | tail -2
  removed=1
fi
if [ -f "$CA_RPM" ]; then
  rm -f "$CA_RPM"
  command -v update-ca-trust >/dev/null 2>&1 && update-ca-trust extract
  removed=1
fi
[ "$removed" = 1 ] && echo "    已移除" || echo "    未找到已安装的 CA"

echo "==> [4/5] 清理 shell 代理变量"
for rc in "$USER_HOME/.zshrc" "$USER_HOME/.bashrc" "$USER_HOME/.profile"; do
  [ -f "$rc" ] || continue
  if grep -q ">>> hublane relay >>>" "$rc"; then
    cp "$rc" "$rc.hublane.bak"
    sed -i '/# >>> hublane relay >>>/,/# <<< hublane <<</d' "$rc"
    echo "    已清理 $rc (备份: $rc.hublane.bak)"
  fi
done

echo "==> [5/5] 安装目录"
if [ "$PURGE" = 1 ]; then
  rm -rf "$DEST"
  echo "    已删除 $DEST (含本地 CA 私钥)"
else
  echo "    保留 $DEST (配置/证书/日志); 彻底删除: sudo rm -rf $DEST"
  echo "    注意: 本地 CA 私钥仍在 $DEST/ca.key, 不再使用请一并删除"
fi

echo
echo "完成。请重开终端或执行:  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY"
