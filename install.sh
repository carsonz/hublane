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
RESET_CONFIG=0
# git push 不走 hublane(它只做匿名只读加速, 见 CHANGELOG 0.1.1 的 Security 条目),
# 应当走 SSH。但受限网络常有两个坑: github.com 的 DNS 被劫持到 127.0.0.1、22 端口
# 被封, 于是 ssh 连到本机 sshd 被拒。故按需把 git@github.com 切到官方的
# ssh.github.com:443 入口。
GIT_SSH_MODE=auto
for arg in "$@"; do
  case "$arg" in
    -y|--yes)     ASSUME_YES=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --reset-config) RESET_CONFIG=1 ;;
    --git-ssh=*)
      GIT_SSH_MODE="${arg#*=}"
      case "$GIT_SSH_MODE" in
        auto|always|never) ;;
        *) echo "错误: --git-ssh 只接受 auto / always / never" >&2; exit 2 ;;
      esac ;;
    --no-git-ssh) GIT_SSH_MODE=never ;;
    -h|--help)
      cat <<'USAGE'
用法: sudo bash install.sh [选项]
  -y, --yes        非交互(不再询问, 按默认继续)
      --dry-run    只打印将执行的步骤, 不做任何改动
      --skip-verify 跳过安装后的三条联网验证
      --reset-config 用发行包里的默认配置**覆盖**已有的 config.json
                  (默认行为是: 保留你的 config.json, 把新版另存为
                   config.json.new, 由你自己合并 —— 升级不会丢配置)
      --git-ssh=auto|always|never
                  是否把 Git 对 GitHub 的访问切到 ssh.github.com:443。
                  auto(默认)= 仅在检测到 github.com 被劫持到本机/私有地址、
                  或 22 端口不通时才写; always= 总是写; never= 不写。
                  --no-git-ssh 等价于 never。
  -h, --help       显示本帮助
USAGE
      exit 0 ;;
    *) echo "未知参数: $arg (用 --help 查看用法)" >&2; exit 2 ;;
  esac
done

if [ "$DRY_RUN" = 1 ]; then
  echo "(dry-run) 将执行: 选定 Python -> 部署到 $DEST(已有 config.json 会保留,"
  echo "(dry-run)         新版另存 config.json.new) -> 生成本地 CA/证书 -> 配置校验 -> "
  echo "(dry-run)         安装 CA 到系统信任库 -> 写入 systemd 服务 -> 注入 shell 代理变量 -> "
  echo "(dry-run)         按需配置 Git 的 GitHub SSH 入口(ssh.github.com:443) -> 验证"
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
# 注意 sudo: $HOME 与 $(id -un) 都是 root, 直接用会把代理变量写进 /root/.zshrc,
# 用户自己的 shell 永远拿不到(uninstall.sh 是按 SUDO_USER 清理的, 写了也清不掉)。
# 这里与 uninstall.sh 保持一致, 认发起安装的那个用户。
USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$USER_NAME" 2>/dev/null | cut -d: -f6)"
[ -n "$USER_HOME" ] || USER_HOME="$HOME"
# 以 passwd 里登记的登录 shell 为准, $SHELL 只作兜底:
# $SHELL 是继承来的进程状态, 不等于用户的登录 shell —— 例如从 IDE 任务、
# CI 或 bash 脚本里 `sudo bash install.sh`, $SHELL 可能是 /bin/bash,
# 于是代理变量被写进 .bashrc, 而用户实际天天用的 zsh 永远读不到。
USER_SHELL="$(getent passwd "$USER_NAME" 2>/dev/null | cut -d: -f7)"
case "${USER_SHELL:-$SHELL}" in
  # zsh 用 .zshenv 而不是 .zshrc: .zshenv 被**所有** zsh 读取(含交互式),
  # 一个文件就覆盖 `zsh -c` 这类非交互场景; 且它在 .zshrc **之前**执行,
  # 用户想临时改代理(比如某个项目走别的节点)只要写在 .zshrc 里就能覆盖默认值。
  # .zshrc 仅交互式读取, 非交互脚本拿不到代理, 会在 CI/脚本里莫名不走代理。
  *zsh)  SHELLRC="$USER_HOME/.zshenv" ;;
  # bash 没有等价物(只有交互式才读 .bashrc), 就按惯例写 .bashrc。
  *bash) SHELLRC="$USER_HOME/.bashrc" ;;
  *)     SHELLRC="$USER_HOME/.profile" ;;
esac
echo "Shell 检测: 登录 shell=${USER_SHELL:-未登记}(继承 \$SHELL=${SHELL:-空}) -> 写入 $SHELLRC"

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
  # CA 必须显式带 keyUsage: openssl req -x509 的默认输出没有它,
  # OpenSSL <= 3.0 容忍, 但 3.5+ 会报 "CA cert does not include key usage extension"
  # 直接拒绝校验 -> 之后所有 HTTPS 都被判不可信。
  # 用 CSR + x509 -signkey 自签而不是 req -x509: req -x509 不接受 -extfile, 只能靠
  # -addext; 而 -addext 在 PyInstaller 冻结产物里调 openssl 会 SIGSEGV(实测, 见
  # tools/build_exe.sh)。与 hublane.py 保持一致(测试会校验三处一致)。
  printf "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n" > "$d/ca.ext"
  openssl req -newkey rsa:2048 -nodes -keyout "$d/ca.key" -out "$d/ca.csr" \
    -subj "/O=hublane/OU=Local Relay/CN=hublane Local Relay CA" 2>/dev/null
  openssl x509 -req -in "$d/ca.csr" -signkey "$d/ca.key" -extfile "$d/ca.ext" \
    -days 3650 -out "$d/ca.crt" 2>/dev/null
  local san="DNS:ajax.googleapis.com,DNS:api.github.com,DNS:assets.hcaptcha.com,DNS:auth.docker.io,DNS:avatars.githubusercontent.com,DNS:bitbucket.org,DNS:camo.githubusercontent.com,DNS:cdn-lfs.huggingface.co,DNS:cdn.arkoselabs.com,DNS:cdn.jsdelivr.net,DNS:cdnjs.cloudflare.com,DNS:client-api.arkoselabs.com,DNS:cloud.githubusercontent.com,DNS:codeberg.org,DNS:codeload.github.com,DNS:conda.anaconda.org,DNS:crates.io,DNS:dl.dropboxusercontent.com,DNS:docs.rs,DNS:downloads.sourceforge.net,DNS:dropbox.com,DNS:epic-games-api.arkoselabs.com,DNS:esm.sh,DNS:files.pythonhosted.org,DNS:fly.dev,DNS:fonts.googleapis.com,DNS:fonts.gstatic.com,DNS:gcr.io,DNS:ghcr.io,DNS:gist.github.com,DNS:github.com,DNS:github.dev,DNS:github.githubassets.com,DNS:githubusercontent.com,DNS:gitlab.com,DNS:go.dev,DNS:golang.google.cn,DNS:golang.org,DNS:gravatar.com,DNS:hcaptcha.com,DNS:hf.co,DNS:huggingface.co,DNS:imgs.hcaptcha.com,DNS:imgs3.hcaptcha.com,DNS:index.crates.io,DNS:js.hcaptcha.com,DNS:k8s.gcr.io,DNS:mega.co.nz,DNS:mega.io,DNS:mega.nz,DNS:netlify.app,DNS:netlify.com,DNS:newassets.hcaptcha.com,DNS:nodejs.org,DNS:objects.githubusercontent.com,DNS:onedrive.live,DNS:onedrive.live.com,DNS:pages.dev,DNS:private-user-images.githubusercontent.com,DNS:prod-ireland.arkoselabs.com,DNS:production.cloudflare.docker.com,DNS:proxy.golang.org,DNS:pypi.org,DNS:quay.io,DNS:railway.app,DNS:raw.githubusercontent.com,DNS:registry-1.docker.io,DNS:registry.k8s.io,DNS:registry.npmjs.org,DNS:releases.hashicorp.com,DNS:repo.anaconda.com,DNS:secure.gravatar.com,DNS:sourceforge.net,DNS:static.crates.io,DNS:static.rust-lang.org,DNS:sum.golang.org,DNS:themes.googleusercontent.com,DNS:unpkg.com,DNS:user-images.githubusercontent.com,DNS:vercel.app,DNS:workers.dev,DNS:www.dropbox.com,DNS:www.github.com,DNS:www.gravatar.com,DNS:www.hcaptcha.com,DNS:www.mega.nz,DNS:www.npmjs.com,DNS:opencode.ai,DNS:www.baidu.com,DNS:localhost"
  openssl req -newkey rsa:2048 -nodes -keyout "$d/server.key" -out "$d/server.csr" \
    -subj "/O=hublane/OU=Local Relay/CN=hublane Relay Leaf" 2>/dev/null
  openssl x509 -req -in "$d/server.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -CAcreateserial \
    -out "$d/server.crt" -days 3650 \
    -extfile <(printf "subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n" "$san") 2>/dev/null
  rm -f "$d/server.csr" "$d/ca.csr" "$d/ca.ext" "$d/ca.srl"
  if ! openssl verify -CAfile "$d/ca.crt" "$d/server.crt" >/dev/null 2>&1; then
    echo "    错误: 生成的证书自校验失败(CA 扩展或 SAN 有问题), 请删除 $d 后重试" >&2
    exit 1
  fi
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

echo "==> [1/7] 选定 Python 解释器"
# 提前到部署之前选好: 下面要先用新代码校验"最终会生效的那份配置",
# 校验不通过就必须在覆盖 hublane.py 之前中止, 否则会留下半安装状态。
PY=""
for cand in python3 /usr/bin/python3 python3.10 /usr/bin/python3.10 python; do
  if PY="$(command -v "$cand" 2>/dev/null)"; then break; fi
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "    错误: 未找到 python3"; exit 1; fi
echo "    Python = $PY  ($("$PY" -V 2>&1))"

echo
echo "==> [2/7] 部署文件到 $DEST"
mkdir -p "$DEST"
gen_certs "$DEST"

# ---- 配置: 已存在则保留, 发行包里的新版另存为 config.json.new ----
# 以前这里是 install -m 0644 "$SRC/config.json" "$DEST/config.json", 无条件覆盖,
# 于是每次升级都会静默丢掉用户的自定义(端口 / token / 上游选择 / 日志设置)。
# 现在: 保留用户那份, 新版另存 .new 让用户自己合并。
CFG="$DEST/config.json"
NEWCFG="$DEST/config.json.new"
if [ -f "$CFG" ] && [ "$RESET_CONFIG" = 0 ]; then
  if cmp -s "$SRC/config.json" "$CFG"; then
    echo "    config.json 与发行版一致, 保持不动"
    rm -f "$NEWCFG"
  else
    cp -p "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
    cp "$SRC/config.json" "$NEWCFG"
    echo "    已保留你现有的 config.json(旧值另存为 config.json.bak-<时间戳>)"
    echo "    发行包里的新版默认配置已写到: $NEWCFG"
    echo "    -> 新增/变更的键请自行合并:  diff $CFG $NEWCFG"
    echo "    -> 想直接用新版:  sudo bash install.sh --reset-config"
  fi
else
  install -m 0644 "$SRC/config.json" "$CFG"
  rm -f "$NEWCFG"
  [ "$RESET_CONFIG" = 1 ] && echo "    已按 --reset-config 覆盖为发行版默认配置"
fi

# 用**新代码**校验"最终会生效的那份配置"; 不通过就到此为止, 不动 hublane.py
if ! "$PY" "$SRC/hublane.py" --config "$CFG" --check; then
  echo >&2
  echo "    错误: 保留下来的 config.json 用新版 hublane 校验不通过, 已中止安装。" >&2
  echo "    你的配置原样未动。请把发行包里的新默认值合并进来再装一次:" >&2
  echo "      diff $CFG $NEWCFG   # 看差异" >&2
  echo "      # 改好后再 sudo bash install.sh" >&2
  exit 1
fi

install -m 0755 "$SRC/hublane.py"   "$DEST/hublane.py"

echo
echo "==> [3/7] 配置校验 (P2)"
"$PY" "$DEST/hublane.py" --config "$CFG" --check || exit 1

echo "==> [4/7] 安装本地 CA 到系统信任库"
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

echo "==> [5/7] 写入 systemd 服务"
cat > /etc/systemd/system/hublane.service <<UNIT
[Unit]
Description=hublane relay proxy for WSL
After=network.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$DEST
ExecStart=$PY $DEST/hublane.py --config $CFG
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
  nohup "$PY" "$DEST/hublane.py" --config "$CFG" >/tmp/hublane.log 2>&1 &
fi

echo "==> [6/7] 配置 shell 代理变量 -> $SHELLRC"
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

echo
echo "==> [7/7] 配置 Git 的 GitHub SSH 入口"
# hublane 只做匿名只读加速, 带凭证的 git push 应当走 SSH(见 CHANGELOG 0.1.1 的
# Security 条目)。但受限网络常把 github.com 劫持到 127.0.0.1 或封掉 22 端口,
# 于是 ssh 连到本机 sshd 被拒 —— 表现为 Permission denied, 却查不出密钥有什么
# 问题。这里检测到异常就把 git@github.com 指向 GitHub 官方的 ssh.github.com:443。
# 以真实用户身份写入(不加 sudo 前缀时 $HOME 就是用户自己的), 已有配置绝不覆盖。
if [ -f "$SRC/tools/setup-git-ssh.sh" ]; then
  bash "$SRC/tools/setup-git-ssh.sh" \
      --home "$USER_HOME" --user "$USER_NAME" --mode "$GIT_SSH_MODE" 2>&1 | sed 's/^/    /'
else
  echo "    [警告] 未找到 tools/setup-git-ssh.sh, 跳过(不影响代理本身)"
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
