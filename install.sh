#!/usr/bin/env bash
# hublane : WSL GitHub 中继代理 安装脚本 (支持 bash / zsh)
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/opt/hublane"
PORT=8899

# 3.2: 非交互 / 试运行
ASSUME_YES=0
DRY_RUN=0
SKIP_VERIFY=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes)     ASSUME_YES=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    -h|--help)
      cat <<'USAGE'
用法: sudo bash install.sh [选项]
  -y, --yes        非交互(不再询问, 按默认继续)
      --dry-run    只打印将执行的步骤, 不做任何改动
      --skip-verify 跳过安装后的三条联网验证
  -h, --help       显示本帮助
USAGE
      exit 0 ;;
    *) echo "未知参数: $arg (用 --help 查看用法)" >&2; exit 2 ;;
  esac
done

if [ "$DRY_RUN" = 1 ]; then
  echo "(dry-run) 将执行: 部署到 $DEST -> 生成本地 CA/证书 -> 配置校验 -> "
  echo "(dry-run)         安装 CA 到系统信任库 -> 写入 systemd 服务 -> 注入 shell 代理变量 -> 验证"
  echo "(dry-run) 未做任何改动。"
  exit 0
fi

if [ "$(id -u)" != "0" ]; then
  echo "需要 root 权限: sudo bash install.sh" >&2
  exit 1
fi

# WSL 自检: 常见问题在这里就暴露出来, 免得装完才发现
if [ -r /proc/version ] && grep -qi microsoft /proc/version 2>/dev/null; then
  echo "==> WSL 自检"
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "    [提示] 无 systemctl, 将回退为 nohup 直启(开机不自启)"
  elif ! systemctl is-system-running >/dev/null 2>&1; then
    echo "    [提示] systemd 未运行(WSL 默认可能关闭), 服务无法开机自启"
    echo "           可在 /etc/wsl.conf 里加 [boot] 段并写 systemd=true, 然后 wsl --shutdown"
  else
    echo "    systemd 可用"
  fi
  if [ -f /etc/resolv.conf ] && grep -q "nameserver" /etc/resolv.conf; then
    echo "    DNS: $(awk '/nameserver/{print $2; exit}' /etc/resolv.conf) (本项目用 DoH, 不依赖它)"
  fi
fi

# ---- 判定要写入哪个 shell 配置 (zsh / bash / 其它) ----
USER_SHELL="$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f7)"
case "${SHELL:-$USER_SHELL}" in
  *zsh)  SHELLRC="$HOME/.zshrc" ;;
  *bash) SHELLRC="$HOME/.bashrc" ;;
  *)     SHELLRC="$HOME/.profile" ;;
esac
echo "Shell 检测: ${SHELL:-$USER_SHELL} -> 写入 $SHELLRC"

# ---- 本地生成 CA + 叶子证书 (不向仓库提交任何私钥) ----
gen_certs() {
  local d="$1"
  if [ -f "$d/ca.crt" ] && [ -f "$d/ca.key" ] && [ -f "$d/server.crt" ] && [ -f "$d/server.key" ]; then
    return 0
  fi
  if ! command -v openssl >/dev/null 2>&1; then
    echo "    错误: 未找到 openssl, 无法生成本地 CA 证书" >&2
    echo "    请先安装 openssl (apt install openssl / brew install openssl)" >&2
    exit 1
  fi
  echo "    生成本地 CA 与叶子证书 (openssl)..."
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$d/ca.key" -out "$d/ca.crt" -days 3650 \
    -subj "/O=hublane/OU=Local Relay/CN=hublane Local Relay CA" 2>/dev/null
  local san="DNS:ajax.googleapis.com,DNS:api.github.com,DNS:assets.hcaptcha.com,DNS:auth.docker.io,DNS:avatars.githubusercontent.com,DNS:bitbucket.org,DNS:camo.githubusercontent.com,DNS:cdn-lfs.huggingface.co,DNS:cdn.arkoselabs.com,DNS:cdn.jsdelivr.net,DNS:cdnjs.cloudflare.com,DNS:client-api.arkoselabs.com,DNS:cloud.githubusercontent.com,DNS:codeberg.org,DNS:codeload.github.com,DNS:conda.anaconda.org,DNS:crates.io,DNS:dl.dropboxusercontent.com,DNS:docs.rs,DNS:downloads.sourceforge.net,DNS:dropbox.com,DNS:epic-games-api.arkoselabs.com,DNS:esm.sh,DNS:files.pythonhosted.org,DNS:fly.dev,DNS:fonts.googleapis.com,DNS:fonts.gstatic.com,DNS:gcr.io,DNS:ghcr.io,DNS:gist.github.com,DNS:github.com,DNS:github.dev,DNS:github.githubassets.com,DNS:githubusercontent.com,DNS:gitlab.com,DNS:go.dev,DNS:golang.google.cn,DNS:golang.org,DNS:gravatar.com,DNS:hcaptcha.com,DNS:hf.co,DNS:huggingface.co,DNS:imgs.hcaptcha.com,DNS:imgs3.hcaptcha.com,DNS:index.crates.io,DNS:js.hcaptcha.com,DNS:k8s.gcr.io,DNS:mega.co.nz,DNS:mega.io,DNS:mega.nz,DNS:netlify.app,DNS:netlify.com,DNS:newassets.hcaptcha.com,DNS:nodejs.org,DNS:objects.githubusercontent.com,DNS:onedrive.live,DNS:onedrive.live.com,DNS:pages.dev,DNS:private-user-images.githubusercontent.com,DNS:prod-ireland.arkoselabs.com,DNS:production.cloudflare.docker.com,DNS:proxy.golang.org,DNS:pypi.org,DNS:quay.io,DNS:railway.app,DNS:raw.githubusercontent.com,DNS:registry-1.docker.io,DNS:registry.k8s.io,DNS:registry.npmjs.org,DNS:releases.hashicorp.com,DNS:repo.anaconda.com,DNS:secure.gravatar.com,DNS:sourceforge.net,DNS:static.crates.io,DNS:static.rust-lang.org,DNS:sum.golang.org,DNS:themes.googleusercontent.com,DNS:unpkg.com,DNS:user-images.githubusercontent.com,DNS:vercel.app,DNS:workers.dev,DNS:www.dropbox.com,DNS:www.github.com,DNS:www.gravatar.com,DNS:www.hcaptcha.com,DNS:www.mega.nz,DNS:www.npmjs.com,DNS:opencode.ai,DNS:www.baidu.com,DNS:localhost"
  openssl req -newkey rsa:2048 -nodes -keyout "$d/server.key" -out "$d/server.csr" \
    -subj "/O=hublane/OU=Local Relay/CN=hublane Relay Leaf" 2>/dev/null
  openssl x509 -req -in "$d/server.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -CAcreateserial \
    -out "$d/server.crt" -days 3650 \
    -extfile <(printf "subjectAltName=%s\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n" "$san") 2>/dev/null
  rm -f "$d/server.csr" "$d/ca.srl"
  chmod 600 "$d/ca.key" "$d/server.key"
  chmod 644 "$d/ca.crt" "$d/server.crt"
}

# 覆盖安装前确认(非交互或 --yes 时跳过)
if [ -f "$DEST/hublane.py" ] && [ "$ASSUME_YES" != "1" ] && [ -t 0 ]; then
  printf "检测到已安装(%s), 覆盖安装? [Y/n] " "$DEST"
  read -r reply
  case "$reply" in
    [nN]*) echo "已取消"; exit 0 ;;
  esac
fi

echo "==> [1/6] 部署文件到 $DEST"
mkdir -p "$DEST"
gen_certs "$DEST"
install -m 0755 "$SRC/hublane.py"   "$DEST/hublane.py"
install -m 0644 "$SRC/config.json"  "$DEST/config.json"

echo "==> [2/6] 选定 Python 解释器"
PY=""
for cand in python3 /usr/bin/python3 python3.10 /usr/bin/python3.10 python; do
  if PY="$(command -v "$cand" 2>/dev/null)"; then break; fi
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "    错误: 未找到 python3"; exit 1; fi
echo "    Python = $PY  ($("$PY" -V 2>&1))"

echo "==> [3/6] 配置校验 (P2)"
"$PY" "$DEST/hublane.py" --config "$DEST/config.json" --check || exit 1

echo "==> [4/6] 安装本地 CA 到系统信任库"
if command -v update-ca-certificates >/dev/null 2>&1; then
  cp "$DEST/ca.crt" /usr/local/share/ca-certificates/hublane-local-ca.crt
  update-ca-certificates 2>&1 | tail -2
elif command -v update-ca-trust >/dev/null 2>&1; then
  cp "$DEST/ca.crt" /etc/pki/ca-trust/source/anchors/hublane-local-ca.crt
  update-ca-trust extract
  echo "    已写入 RPM 系信任库 (update-ca-trust)"
else
  echo "    [警告] 未找到 update-ca-certificates / update-ca-trust"
  echo "           请手动信任 $DEST/ca.crt, 否则 HTTPS 会被浏览器/curl 判为不可信"
fi

echo "==> [5/6] 写入 systemd 服务"
cat > /etc/systemd/system/hublane.service <<UNIT
[Unit]
Description=hublane relay proxy for WSL
After=network.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$DEST
ExecStart=$PY $DEST/hublane.py --config $DEST/config.json
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload 2>/dev/null
if systemctl enable --now hublane >/dev/null 2>&1; then
  echo "    systemd: 已启用并启动"
else
  echo "    systemd 不可用, 回退为 nohup 直启"
  pkill -f hublane.py 2>/dev/null || true
  nohup "$PY" "$DEST/hublane.py" --config "$DEST/config.json" >/tmp/hublane.log 2>&1 &
fi

echo "==> [6/6] 配置 shell 代理变量 -> $SHELLRC"
touch "$SHELLRC"
if ! grep -q "hublane" "$SHELLRC" 2>/dev/null; then
cat >> "$SHELLRC" <<'RC'

# >>> hublane relay >>>
export http_proxy="http://127.0.0.1:8899"
export https_proxy="http://127.0.0.1:8899"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export NO_PROXY="localhost,127.0.0.1,::1,.local"
export no_proxy="$NO_PROXY"
# <<< hublane <<<
RC
  echo "    已追加到 $SHELLRC"
else
  echo "    已存在, 跳过"
fi

sleep 3
echo
if [ "$SKIP_VERIFY" = "1" ]; then
  echo "==> 已跳过联网验证 (--skip-verify)"
else
echo "==> 验证 (-x 显式指定代理, 与是否重开 shell 无关)"
printf "   baidu      : "; curl -x "http://127.0.0.1:$PORT" -fsSL --max-time 40 -o /dev/null -w "%{http_code}  %{time_total}s\n" https://www.baidu.com 2>&1 | tail -1
printf "   hermes raw : "; curl -x "http://127.0.0.1:$PORT" -fsSL --max-time 90 -o /tmp/_hl_hermes.sh -w "%{http_code}  %{size_download}B  %{time_total}s\n" https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh 2>&1 | tail -1
printf "   opencode   : "; curl -x "http://127.0.0.1:$PORT" -fsSL --max-time 90 -o /tmp/_hl_oc.sh -w "%{http_code}  %{size_download}B  %{time_total}s\n" https://opencode.ai/install 2>&1 | tail -1
echo "   hermes 首行: $(head -1 /tmp/_hl_hermes.sh 2>/dev/null)"
echo "   openco 首行: $(head -1 /tmp/_hl_oc.sh 2>/dev/null)"
fi
echo
echo "==> 运维入口"
echo "   面板     : http://127.0.0.1:28898/          (HTML, 5 秒自动刷新)"
echo "   指标/PAC : curl http://127.0.0.1:28898/status   |   /pac"
echo "   日志     : sudo journalctl -u hublane -f"
echo "   重启     : sudo systemctl restart hublane"
echo "   卸载     : sudo bash $SRC/uninstall.sh   (加 --purge 连目录一起删)"
echo
echo "完成。请执行:  exec ${SHELL##*/}     (或重开终端)"
