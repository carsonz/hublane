#!/usr/bin/env bash
# 为 Git 配置 GitHub 的 SSH 443 入口, 绕开 DNS 劫持与 22 端口封锁。
#
# 背景(实测): 某些环境(例如 Windows 侧的代理/加速工具)会把 github.com 的 DNS
# 指向 127.0.0.1, 于是 `ssh git@github.com` 连到的是**本机 sshd**, 必然被拒,
# 表现为 "Permission denied (publickey)" —— 看起来像密钥没配好, 其实请求根本没出
# 本机(ssh -v 里能看到 `Connecting to github.com [127.0.0.1]`)。
# 另外受限网络常封 22 端口。GitHub 官方提供 ssh.github.com:443 作为 SSH 入口,
# 既绕开被劫持的域名, 又走通常放行的 443, 是官方支持的做法。
#
# 策略:
#   - 默认 auto: 只在检测到异常(DNS 被劫持 / 22 端口不通)时才写入;
#   - 只**追加** ~/.ssh/config 里一段带标记的配置, 不动你已有的任何内容;
#   - 标记存在就跳过, 重复执行无副作用(幂等)。
#
# 用法:
#   bash tools/setup-git-ssh.sh                 # 自动检测
#   bash tools/setup-git-ssh.sh --mode always   # 不看检测结果, 直接写
#   bash tools/setup-git-ssh.sh --mode never    # 只检测, 不写
#   bash tools/setup-git-ssh.sh --dry-run
#   sudo bash tools/setup-git-ssh.sh            # 由 install.sh 以 root 调用时,
#                                               # 自动定位到 SUDO_USER 的家目录
#   bash tools/setup-git-ssh.sh --home /home/zsy --user zsy
#
# 回滚: 删掉 ~/.ssh/config 里两个 hublane 标记之间的内容即可。
set -uo pipefail

MODE=auto
DRY_RUN=0
WANT_HOME=""
WANT_USER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)   MODE="${2:-auto}"; shift 2 ;;
    --mode=*) MODE="${1#*=}"; shift ;;
    --home)   WANT_HOME="${2:-}"; shift 2 ;;
    --user)   WANT_USER="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $1 (用 --help 查看用法)" >&2; exit 2 ;;
  esac
done

case "$MODE" in
  auto|always|never) ;;
  *) echo "错误: --mode 只接受 auto / always / never" >&2; exit 2 ;;
esac

# ---- 定位目标用户的家目录 ----
# sudo 调用时 $HOME 是 /root, 写进去用户自己的 ssh 根本读不到(与 install.sh
# 处理 shell rc 文件是同一个坑, 那里也是认 SUDO_USER)。
if [ -z "$WANT_USER" ] && [ "$(id -u)" = "0" ] && [ -n "${SUDO_USER:-}" ]; then
  WANT_USER="$SUDO_USER"
fi
HOME_DIR="$WANT_HOME"
if [ -z "$HOME_DIR" ] && [ -n "$WANT_USER" ]; then
  HOME_DIR="$(getent passwd "$WANT_USER" 2>/dev/null | cut -d: -f6)"
fi
[ -n "$HOME_DIR" ] || HOME_DIR="${HOME:-/root}"
[ -n "$WANT_USER" ] || WANT_USER="$(id -un)"

is_private_ip() {
  local ip="$1"
  [ -n "$ip" ] || return 0            # 解析不出来也按异常处理
  case "$ip" in
    127.*|10.*|192.168.*|169.254.*|0.*) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 0 ;;
    ::1|fc*:*|fd*:*|fe80:*) return 0 ;;
  esac
  return 1
}

resolve_ip() { getent hosts "$1" 2>/dev/null | awk '{print $1; exit}'; }

port_open() {
  # 只在 IP 是公网地址时才探测: DNS 被劫持时 /dev/tcp 会连到本机 sshd 而误判为"通"
  timeout 3 bash -c ":</dev/tcp/$1/$2" 2>/dev/null
}

GH_IP="$(resolve_ip github.com)"
ALT_IP="$(resolve_ip ssh.github.com)"

NEED=0
REASON=""
if [ -z "$GH_IP" ]; then
  NEED=1; REASON="github.com 无法解析"
elif is_private_ip "$GH_IP"; then
  NEED=1; REASON="github.com 被解析到 $GH_IP(本机或私有地址), DNS 遭劫持"
elif ! port_open github.com 22; then
  NEED=1; REASON="github.com:22 不通(网络封锁 22 端口或 SSH 被拦)"
fi

echo "==> 检测 GitHub SSH 连通性"
echo "    github.com      -> ${GH_IP:-解析失败}"
echo "    ssh.github.com  -> ${ALT_IP:-解析失败}"
if [ "$NEED" = 1 ]; then
  echo "    判定: 需要走 443 入口 ($REASON)"
else
  echo "    判定: 直连 22 端口正常, 无需改动"
fi

if [ "$MODE" = never ]; then
  echo "    --mode never: 只检测, 未写入。"
  exit 0
fi
if [ "$MODE" = auto ] && [ "$NEED" = 0 ]; then
  echo "    未检测到问题, 跳过(想强制写入用 --mode always)。"
  exit 0
fi

SSH_DIR="$HOME_DIR/.ssh"
CFG="$SSH_DIR/config"
BEGIN="# >>> hublane: GitHub SSH over 443 >>>"
END="# <<< hublane: GitHub SSH over 443 <<<"

if grep -qF "$BEGIN" "$CFG" 2>/dev/null; then
  echo "==> $CFG 已含本段配置, 跳过(幂等)"
  exit 0
fi

echo "==> 写入 $CFG"
if [ "$DRY_RUN" = 1 ]; then
  echo "    (dry-run) 将追加:"
  echo "      Host github.com -> ssh.github.com:443, User git"
  exit 0
fi

mkdir -p "$SSH_DIR"
[ -f "$CFG" ] || : > "$CFG"
# 原文件若不以换行结尾, 标记会被拼到上一行末尾 -> 先补一个换行
if [ -s "$CFG" ] && [ -n "$(tail -c1 "$CFG")" ]; then
  printf '\n' >> "$CFG"
fi

cat >> "$CFG" <<EOF

$BEGIN
# 由 hublane 写入。原因: github.com 的 DNS 被劫持到本机/私有地址，或网络封锁 22
# 端口，导致 ssh 连不到真正的 GitHub。改用 GitHub 官方的 ssh.github.com:443 入口。
# 删掉两个标记之间的内容即可恢复默认行为。
Host github.com
  HostName ssh.github.com
  Port 443
  User git
$END
EOF

chmod 700 "$SSH_DIR" 2>/dev/null || true
chmod 600 "$CFG"
# 以 root 跑时(install.sh)新建的文件属主是 root, 用户自己读不到
if [ "$(id -u)" = 0 ] && [ -n "$WANT_USER" ]; then
  chown "$WANT_USER" "$SSH_DIR" "$CFG" 2>/dev/null || true
fi
echo "    已追加(原有内容未改动)"

# ---- 验证 ----
# 只有"目标家目录 == 当前用户家目录"时握手才有意义: 以 root 跑会去读 root 的
# ssh 配置, 验的就不是用户的那份了。
if [ "$HOME_DIR" = "${HOME:-}" ] && command -v ssh >/dev/null 2>&1; then
  echo "    验证(需已把 SSH 公钥加到 GitHub):"
  # ssh -T 认证成功后仍返回 1(GitHub 不提供 shell), 配合 pipefail 会让本脚本
  # 以 1 退出, 把 install.sh 的返回码也带歪 —— 故显式吞掉。
  timeout 20 ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
      -T git@github.com 2>&1 | head -2 | sed 's/^/      /' || true
else
  echo "    请自行验证: ssh -T git@github.com"
fi
