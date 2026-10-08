#!/usr/bin/env bash
# 在无 root 权限的环境里验证 install.sh 的逻辑(WSL/Ubuntu)。
#
# 为什么需要: install.sh 要写 /opt、/etc/systemd、/usr/local/share/ca-certificates,
# 没root 就没法真跑; 而它此前从未被任何自动化测试覆盖, 改坏了也不会有人知道。
# 本脚本把 install.sh 的路径常量重定向到临时沙箱, 并用桩替换 systemctl /
# update-ca-certificates, 于是证书生成、配置校验、unit 文件内容、shell rc 注入、
# 清理临时文件这些逻辑都能被真实执行与断言。
#
# 注意: 沙箱验证的是**脚本逻辑**; 真正的 systemd 启动、update-ca-certificates
# 信任库写入、开机自启仍需在有 root 的机器上跑一次 `sudo bash install.sh -y`。
#
# 用法: bash tools/verify_install_sandbox.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SB="$(mktemp -d -t hublane-instest-XXXXXX)"
FAILED=0
trap 'rm -rf "$SB"' EXIT

ok()   { echo "  [PASS] $*"; }
bad()  { echo "  [FAIL] $*"; FAILED=1; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

echo "沙箱: $SB"
mkdir -p "$SB"/{opt,etc/systemd,ca-certificates,home,stub} "$SB/home"

# ---- 桩: 记录调用, 不真的动系统 ----
cat > "$SB/stub/systemctl" <<'EOF'
#!/bin/sh
echo "[stub systemctl] $*" >> "$SANDBOX_LOG"
case "$1" in
  is-system-running) exit 0 ;;
esac
exit 0
EOF
cat > "$SB/stub/update-ca-certificates" <<'EOF'
#!/bin/sh
echo "[stub update-ca-certificates] $*" >> "$SANDBOX_LOG"
exit 0
EOF
chmod +x "$SB/stub/systemctl" "$SB/stub/update-ca-certificates"

# getent 桩: 让桩用户"在 passwd 里有条目", 以便验证登录 shell 判定。
# SHELL_OF_STUB 可控制它登记的登录 shell; 留空则模拟"查不到条目"。
cat > "$SB/stub/getent" <<EOF
#!/bin/sh
# 仅回答 passwd <user>: 命中桩用户时输出 "name:x:uid:gid:gecos:home:shell"
if [ "\$1" = "passwd" ] && [ -n "\$SHELL_OF_STUB" ] && [ "\$2" = "\$STUB_USER" ]; then
  echo "\$STUB_USER:x:4242:4242:stub:\$STUB_HOME:\$SHELL_OF_STUB"
  exit 0
fi
exec /usr/bin/getent "\$@"
EOF
chmod +x "$SB/stub/getent"

# ---- 把 install.sh 的路径常量重定向到沙箱, 并去掉 root 检查 ----
sed -e "s|^DEST=\"/opt/hublane\"|DEST=\"$SB/opt/hublane\"|" \
    -e "s|/etc/systemd/system/hublane.service|$SB/etc/systemd/hublane.service|" \
    -e "s|/usr/local/share/ca-certificates|$SB/ca-certificates|" \
    -e 's|if \[ "$(id -u)" != "0" \]; then|if false; then|' \
    "$REPO/install.sh" > "$SB/install.sh"
cp "$REPO/hublane.py" "$REPO/config.json" "$SB/"

export SANDBOX_LOG="$SB/calls.log"
: > "$SANDBOX_LOG"

# 桩用户: 默认 passwd 里查不到 -> 脚本会回落到 $HOME, 从而完全不碰真实家目录。
# STUB_USER 必须 export: 下面的 getent 桩要在子进程里按名字比对。
STUB_USER="hublane-sandbox-$$"
export STUB_USER SUDO_USER="$STUB_USER"

# ---- 记录真实家目录里 rc 相关文件的初始状态 ----
# 用来证明"沙箱跑完没有碰过真实家目录"。不能直接断言某个 .bak 不存在:
# 用户自己跑过 uninstall.sh --purge 就会留下 ~/.zshrc.hublane.bak, 这与被测代码
# 无关, 却会让断言变成偶发失败(实测踩过)。所以改成跑前快照、跑后对比,
# 只关心"本次沙箱有没有改动真实家目录"。
snapshot_real_home() {
  find /home -maxdepth 2 \
       \( -name ".zshenv" -o -name ".zshrc" -o -name ".bashrc" -o -name ".profile" \
          -o -name "*.hublane.bak" \) 2>/dev/null \
    | sort | while read -r f; do
        printf '%s %s %s\n' "$f" "$(stat -c %s "$f" 2>/dev/null || echo -)" \
                          "$(stat -c %Y "$f" 2>/dev/null || echo -)"
      done
}
REAL_HOME_BEFORE="$SB/real_home.before"
snapshot_real_home > "$REAL_HOME_BEFORE"

echo
echo "== 1. 参数处理 =="
out=$(cd "$SB" && bash install.sh --help); check "--help 打印用法" \
  "[[ \"\$out\" == *\"--dry-run\"* ]]"
(cd "$SB" && bash install.sh --nope >/dev/null 2>&1); check "未知参数 exit 2" "[ \$? -eq 2 ]"
out=$(cd "$SB" && bash install.sh --dry-run); check "--dry-run 不落地任何文件" \
  "[[ \"\$out\" == *\"未做任何改动\"* && ! -e \"$SB/opt/hublane\" ]]"

echo "== 1b. --reset-config 在 --help 里有说明 =="
out=$(cd "$SB" && bash install.sh --help); check "--help 提到 --reset-config" \
  "[[ \"\$out\" == *\"--reset-config\"* ]]"

echo
echo "== 2. 完整安装 (systemd 可用路径) =="
# 用一个 passwd 里不存在的桩用户当 SUDO_USER: install.sh / uninstall.sh 会按
# SUDO_USER 定位家目录, 若用真实用户就会污染本人~/.zshenv。
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB/home" SUDO_USER="$STUB_USER" \
  bash install.sh -y --skip-verify) > "$SB/install.out" 2>&1
rc=$?
if [ $rc -eq 0 ]; then ok "install.sh 退出码 0"; else
  bad "install.sh 退出码 $rc"; sed -n '1,40p' "$SB/install.out"
fi

DEST="$SB/opt/hublane"
UNIT="$SB/etc/systemd/hublane.service"

check "hublane.py 已部署且可执行" "[ -x '$DEST/hublane.py' ]"
check "config.json 已部署"          "[ -f '$DEST/config.json' ]"
check "CA/叶证书齐全"                "[ -f '$DEST/ca.crt' ] && [ -f '$DEST/ca.key' ] && [ -f '$DEST/server.crt' ] && [ -f '$DEST/server.key' ]"
check "临时文件已清理(ca.csr/ca.ext/server.csr)" \
  "[ ! -e '$DEST/ca.csr' ] && [ ! -e '$DEST/ca.ext' ] && [ ! -e '$DEST/server.csr' ] && [ ! -e '$DEST/server.key.tmp' ]"
check "私钥权限 600"                 "[ \"\$(stat -c '%a' '$DEST/ca.key')\" = 600 ] && [ \"\$(stat -c '%a' '$DEST/server.key')\" = 600 ]"

echo
echo "== 3. 证书正确性 (回归: CA 缺 keyUsage 会被 OpenSSL 3.5+ 拒绝) =="
if openssl verify -CAfile "$DEST/ca.crt" "$DEST/server.crt" >/dev/null 2>&1; then
  ok "证书链 openssl verify 通过"
else
  bad "证书链 verify 失败"
fi
for ext in "CA:TRUE" "Certificate Sign"; do
  check "CA 含 '$ext'" "openssl x509 -in '$DEST/ca.crt' -noout -text | grep -q '$ext'"
done
check "叶证书含 60+ 条 SAN" \
  "[ \"\$(openssl x509 -in '$DEST/server.crt' -noout -text | grep -o 'DNS:' | wc -l)\" -ge 60 ]"
check "叶证书含 keyUsage" \
  "openssl x509 -in '$DEST/server.crt' -noout -text | grep -q 'Digital Signature'"
check "CA 已复制到信任库目录"  "[ -f '$SB/ca-certificates/hublane-local-ca.crt' ]"
check "调用了 update-ca-certificates" "grep -q update-ca-certificates '$SANDBOX_LOG'"

echo
echo "== 4. systemd unit 文件 =="
check "unit 文件已写入" "[ -f '$UNIT' ]"
if [ -f "$UNIT" ]; then
  grep -q "ExecStart=.*hublane.py --config $DEST/config.json" "$UNIT" \
    && ok "ExecStart 指向沙箱内的解释器与配置" || bad "ExecStart 不正确"
  grep -q "^Restart=always" "$UNIT" && ok "Restart=always" || bad "缺少 Restart=always"
  grep -q "^WantedBy=multi-user.target" "$UNIT" && ok "WantedBy=multi-user.target" \
    || bad "缺少 WantedBy"
  # 解释器必须来自 install.sh 第2 步选出的那个, 不能是写死的
  PY_ACTUAL=$(grep -oP '(?<=^ExecStart=)\S+' "$UNIT")
  PY_CHOSEN=$(grep -oP 'Python = \K\S+' "$SB/install.out" | head -1)
  [ "$PY_ACTUAL" = "$PY_CHOSEN" ] \
    && ok "unit 解释器与探测结果一致 ($PY_ACTUAL)" \
    || bad "unit 解释器 $PY_ACTUAL != 探测结果 $PY_CHOSEN"
fi
check "调用了 systemctl daemon-reload/enable --now" \
  "grep -q 'daemon-reload' '$SANDBOX_LOG' && grep -q 'enable --now hublane' '$SANDBOX_LOG'"

echo
echo "== 5. shell 代理变量注入 =="
RCFILE=$(find "$SB/home" -name ".zshenv" -o -name ".zshrc" -o -name ".bashrc" -o -name ".profile" | head -1)
check "写入了 rc 文件 ($RCFILE)" "[ -n '$RCFILE' ]"
if [ -n "$RCFILE" ]; then
  for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
    grep -q "$v=" "$RCFILE" && ok "rc 含 $v" || bad "rc 缺 $v"
  done
  grep -q "hublane relay" "$RCFILE" && ok "rc 有 hublane 标记块" || bad "rc 缺标记块"
  # 幂等: 再装一次不应重复追加
  before=$(grep -c "hublane relay" "$RCFILE")
  (cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB/home" SUDO_USER="$STUB_USER" \
    bash install.sh -y --skip-verify) >> "$SB/install.out" 2>&1
  after=$(grep -c "hublane relay" "$RCFILE")
  [ "$before" = "$after" ] && ok "重复安装不重复注入 (幂等)" || bad "重复安装又追加了一遍"
fi

echo
echo "== 5b. sudo 场景: 目标必须是发起安装的用户, 不是 root =="
SB2="$SB/sudo"; mkdir -p "$SB2/root_home"
: > "$SANDBOX_LOG"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB2/root_home" SUDO_USER="$STUB_USER" \
  bash install.sh -y --skip-verify) > "$SB/sudo.out" 2>&1
check "桩用户无passwd 条目时回落到 \$HOME(不碰真实家目录)" \
  "grep -q '写入 $SB2/root_home' '$SB/sudo.out'"
check "未尝试写入真实家目录路径" \
  "! grep -q '写入 /home/' '$SB/sudo.out'"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB2/root_home" SUDO_USER="$STUB_USER" \
  bash install.sh --dry-run) > "$SB/sudo_dry.out" 2>&1
check "sudo 下 --dry-run 仍不落地" "grep -q '未做出改动' '$SB/sudo_dry.out' || grep -q '未做任何改动' '$SB/sudo_dry.out'"

echo
echo "== 5c. 登录 shell 判定: 以 passwd 为准, 不被继承的 \$SHELL 带偏 =="
# 真实场景(2026-10-08 踩到): 用户的登录 shell 是 zsh, 但从 IDE 任务 / CI / bash
# 脚本里 `sudo bash install.sh` 时继承的 $SHELL 是 /bin/bash。
# 若按 $SHELL 判定, 代理变量会写进 .bashrc, 用户天天用的 zsh 永远读不到。
SB3="$SB/shellpick"; mkdir -p "$SB3/home"
: > "$SANDBOX_LOG"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB3/home" SUDO_USER="$STUB_USER" \
  SHELL_OF_STUB="/usr/bin/zsh" STUB_HOME="$SB3/home" SHELL="/bin/bash" \
  bash install.sh -y --skip-verify) > "$SB3/out" 2>&1
check "登录 shell=zsh + 继承 \$SHELL=bash -> 写入 .zshenv" \
  "grep -q '写入 $SB3/home/.zshenv' '$SB3/out'"
check "代理变量确实进了 .zshenv" \
  "grep -q 'http_proxy' '$SB3/home/.zshenv' 2>/dev/null"
check "没有误写 .bashrc" \
  "! grep -q 'http_proxy' '$SB3/home/.bashrc' 2>/dev/null"
check "没有多余的 .zshrc(.zshenv 已覆盖交互式)" \
  "! grep -q 'http_proxy' '$SB3/home/.zshrc' 2>/dev/null"
check "日志同时打印登录 shell 与继承 \$SHELL(便于排查)" \
  "grep -q '登录 shell=/usr/bin/zsh' '$SB3/out'"

# 反向: 登录 shell 是 bash 时就该写 .bashrc
SB4="$SB/shellpick2"; mkdir -p "$SB4/home"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB4/home" SUDO_USER="$STUB_USER" \
  SHELL_OF_STUB="/bin/bash" STUB_HOME="$SB4/home" SHELL="/usr/bin/zsh" \
  bash install.sh -y --skip-verify) > "$SB4/out" 2>&1
check "登录 shell=bash + 继承 \$SHELL=zsh -> 写入 .bashrc" \
  "grep -q '写入 $SB4/home/.bashrc' '$SB4/out'"
check "bash 场景不该碰 .zshenv" \
  "! grep -q 'http_proxy' '$SB4/home/.zshenv' 2>/dev/null"

# 未登记登录 shell 时回落到 $SHELL, 再不行才用 .profile
SB5="$SB/shellpick3"; mkdir -p "$SB5/home"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB5/home" SUDO_USER="$STUB_USER" \
  SHELL_OF_STUB="" STUB_HOME="" SHELL="/bin/bash" \
  bash install.sh -y --skip-verify) > "$SB5/out" 2>&1
check "登录 shell 未登记时回落到继承的 \$SHELL=bash" \
  "grep -q '写入 $SB5/home/.bashrc' '$SB5/out'"
check "回退时日志标明登录 shell 未登记" \
  "grep -q '登录 shell=未登记' '$SB5/out'"

echo
echo "== 6. systemd 不可用时的 nohup 回退 =="
cat > "$SB/stub/systemctl" <<'EOF'
#!/bin/sh
echo "[stub systemctl] $*" >> "$SANDBOX_LOG"
case "$1" in
  is-system-running) exit 1 ;;
esac
exit 1
EOF
chmod +x "$SB/stub/systemctl"
out=$(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB/home" SUDO_USER="$STUB_USER" \
  bash install.sh -y --dry-run 2>&1)
check "systemd 不可用时 dry-run 仍安全" "[[ \"\$out\" == *\"未做任何改动\"* ]]"
# 恢复成"systemd 可用", 供第 7 步卸载
cat > "$SB/stub/systemctl" <<'EOF'
#!/bin/sh
echo "[stub systemctl] $*" >> "$SANDBOX_LOG"
case "$1" in
  is-system-running) exit 0 ;;
esac
exit 0
EOF
chmod +x "$SB/stub/systemctl"

echo
echo "== 6b. 升级不覆盖已有config.json =="
# 回归: 以前是 install -m 0644 config.json DEST/config.json 无条件覆盖,
# 于是每次升级都静默丢掉用户的自定义(端口/token/上游选择/日志设置)。
SB7="$SB/upgrade"; mkdir -p "$SB7/home"
run_install7() {
  (cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB7/home" SUDO_USER="$STUB_USER" \
    SHELL_OF_STUB="/usr/bin/zsh" STUB_HOME="$SB7/home" SHELL="/bin/bash" \
    bash install.sh -y --skip-verify ${1:-}) > "$SB7/out" 2>&1
}
run_install7
CFG7="$SB/opt/hublane/config.json"
check "首次安装生成了 config.json" "[ -f '$CFG7' ]"

# 模拟用户改过配置
python3 - "$CFG7" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
c["listen_port"] = 18899
c["metrics_token"] = "my-own-token"
json.dump(c, open(p, "w"), indent=2)
PY
# 模拟"这次发行包换了默认值": 改沙箱里的发行包副本($SB/config.json,
# install.sh 的 SRC 就是 $SB), 绝不碰仓库里的那份
cp "$REPO/config.json" "$SB/config.json.orig"
python3 - "$SB/config.json" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
c["github_upstreams"] = ["ghproxy_com_gh", "direct"]
json.dump(c, open(p, "w"), indent=2)
PY
run_install7
check "升级后用户的 listen_port 仍在" \
  "python3 -c \"import json;print(json.load(open('$CFG7'))['listen_port'])\" | grep -q 18899"
check "升级后用户的 metrics_token 仍在" \
  "grep -q my-own-token '$CFG7'"
check "发行包的新默认值另存为 config.json.new" \
  "[ -f '$CFG7.new' ] && grep -q ghproxy_com_gh '$CFG7.new'"
check "旧配置留了备份" \
  "ls '$CFG7'.bak-* >/dev/null 2>&1"
check "日志提示了合并方式" \
  "grep -q 'config.json.new' '$SB7/out'"

# --reset-config 应覆盖回发行版默认
run_install7 --reset-config
check "--reset-config 覆盖为发行版默认" \
  "[ ! -f '$CFG7.new' ] && ! grep -q my-own-token '$CFG7'"
mv "$SB/config.json.orig" "$SB/config.json"    # 还原沙箱发行包副本

echo
echo "== 7. uninstall.sh 能把 install.sh 写的变量清掉 =="
sed -e "s|^DEST=\"/opt/hublane\"|DEST=\"$SB/opt/hublane\"|" \
    -e "s|^CA_DEB=.*|CA_DEB=\"$SB/ca-certificates/hublane-local-ca.crt\"|" \
    -e "s|rm -f /etc/systemd/system/hublane.service|rm -f $SB/etc/systemd/hublane.service|" \
    -e 's|if \[ "$(id -u)" != "0" \]; then|if false; then|' \
    "$REPO/uninstall.sh" > "$SB/uninstall.sh"
# 安装写哪个 rc 取决于被测进程的 $SHELL(桩用户默认无 passwd 条目), 所以卸载**之前**
# 先把真正被写入的那个文件记下来。不能用 grep -r 去找: 卸载会生成 .hublane.bak,
# 备份里本来就保留着原始内容, 会把断言指向错误的文件。
UNINST_RC=""
for f in .zshenv .zshrc .bashrc .profile; do
  if [ -f "$SB/home/$f" ] && grep -q 'hublane relay' "$SB/home/$f" 2>/dev/null; then
    UNINST_RC="$SB/home/$f"; break
  fi
done
check "安装确实写到了某个 rc"        "[ -n '$UNINST_RC' ]"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB/home" SUDO_USER="$STUB_USER" \
  bash uninstall.sh) > "$SB/uninstall.out" 2>&1
check "卸载完成"                     "grep -q '完成' '$SB/uninstall.out'"
check "rc 中的 hublane 块已移除"      "[ -n '$UNINST_RC' ] && ! grep -q 'hublane relay' '$UNINST_RC'"
check "rc 中的代理变量已移除"        "[ -n '$UNINST_RC' ] && ! grep -q 'http_proxy' '$UNINST_RC'"
check "rc 已备份"                    "[ -n '$UNINST_RC' ] && [ -f '$UNINST_RC.hublane.bak' ]"
check "CA 已从信任库移除"            "[ ! -f '$SB/ca-certificates/hublane-local-ca.crt' ]"
check "unit 文件已删除"              "[ ! -f '$SB/etc/systemd/hublane.service' ]"
check "非 --purge 时保留安装目录"    "[ -f '$SB/opt/hublane/hublane.py' ]"
check "卸载后 ca.key仍在(未 purge)"   "[ -f '$SB/opt/hublane/ca.key' ]"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB/home" SUDO_USER="$STUB_USER" \
  bash uninstall.sh --purge) >> "$SB/uninstall.out" 2>&1
check "--purge 删除安装目录(含私钥)" "[ ! -d '$SB/opt/hublane' ]"

echo
echo "== 7b. zsh 场景: 写 .zshenv, 卸载能清干净 =="
SB6="$SB/zshenv"; mkdir -p "$SB6/home"
(cd "$SB" && PATH="$SB/stub:$PATH" HOME="$SB6/home" SUDO_USER="$STUB_USER" \
  SHELL_OF_STUB="/usr/bin/zsh" STUB_HOME="$SB6/home" SHELL="/bin/bash" \
  bash install.sh -y --skip-verify) > "$SB6/install.out" 2>&1
check "zsh 登录 shell -> 写入 .zshenv" \
  "grep -q '写入 $SB6/home/.zshenv' '$SB6/install.out'"
check ".zshenv 里代理变量齐全" \
  "grep -q 'https_proxy' '$SB6/home/.zshenv' && grep -q 'no_proxy' '$SB6/home/.zshenv'"
sed -e "s|^DEST=\"/opt/hublane\"|DEST=\"$SB/opt/hublane\"|" \
    -e "s|^CA_DEB=.*|CA_DEB=\"$SB/ca-certificates/hublane-local-ca.crt\"|" \
    -e "s|rm -f /etc/systemd/system/hublane.service|rm -f $SB/etc/systemd/hublane.service|" \
    -e 's|if \[ "$(id -u)" != "0" \]; then|if false; then|' \
    "$REPO/uninstall.sh" > "$SB6/uninstall.sh"
(cd "$SB6" && PATH="$SB/stub:$PATH" HOME="$SB6/home" SUDO_USER="$STUB_USER" \
  SHELL_OF_STUB="/usr/bin/zsh" STUB_HOME="$SB6/home" \
  bash uninstall.sh --purge) > "$SB6/uninstall.out" 2>&1
check "卸载清掉了 .zshenv 里的块"    "! grep -q 'hublane relay' '$SB6/home/.zshenv'"
check "卸载清掉了 .zshenv 里的变量"  "! grep -q 'http_proxy' '$SB6/home/.zshenv'"
check ".zshenv 已备份"               "[ -f '$SB6/home/.zshenv.hublane.bak' ]"

echo
echo "== 8. 真实家目录在整个过程中未被改动 =="
snapshot_real_home > "$SB/real_home.after"
check "真实家目录 rc 文件无新增/删除/改动(跑前跑后快照一致)" \
  "diff -q '$REAL_HOME_BEFORE' '$SB/real_home.after' >/dev/null"
if ! diff -q "$REAL_HOME_BEFORE" "$SB/real_home.after" >/dev/null 2>&1; then
  echo "    被改动的文件:"
  diff "$REAL_HOME_BEFORE" "$SB/real_home.after" | grep -E '^[<>]' | sed 's/^/      /'
fi

echo
echo
echo "==================================="
if [ "$FAILED" = 0 ]; then
  echo "install.sh 沙箱验证: 全部通过"
else
  echo "install.sh 沙箱验证: 存在失败项"
fi
echo "提醒: 真机 systemd 启动 / 信任库生效 仍需 sudo bash install.sh -y 实跑一次。"
exit $FAILED