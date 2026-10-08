#!/usr/bin/env bash
# hublane 可执行文件打包 (PyInstaller -> 单文件, 免安装 Python)
#
# 用法:
#
# 范围: **Linux/WSL** 产物。打 Windows exe 请用 tools/build-exe.py
# (它额外处理证书预置与 --add-data 播种; 本脚本负责 apt 依赖与冻结冒烟)。
#   bash tools/build_exe.sh                 # 按当前平台打包
#   bash tools/build_exe.sh --smoke         # 打包后跑冒烟(会真的起进程验证)
#   bash tools/build_exe.sh --install-deps  # 先用 apt 装齐系统依赖再打包
#
# 现状(2026-10): hublane 核心是零第三方依赖的纯标准库单文件, 因此冻结可行;
# 但 Windows 产物体积/杀软误报/`--service` 是否可用**必须在 Windows 上验证**,
# 详见 docs/EVALUATIONS.md 第1 节与 ROADMAP v0.2.0 第 2 条。
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$SRC/dist/exe"
SMOKE=0
INSTALL_DEPS=0
PY="${PY:-python3}"

for arg in "$@"; do
  case "$arg" in
    --smoke)        SMOKE=1 ;;
    --install-deps) INSTALL_DEPS=1 ;;
    -h|--help)      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

# ---- 系统依赖: 缺失的用 apt 装齐(WINDOWS 用 winget/choco, 见 install-windows.bat) ----
APT_PACKAGES="openssl ca-certificates"   # openssl=生成 CA/叶证书
if [ "$INSTALL_DEPS" = 1 ]; then
  echo "==> 检查系统依赖"
  MISSING=""
  command -v openssl >/dev/null 2>&1 || MISSING="$MISSING openssl"
  [ -d /usr/share/ca-certificates ] || MISSING="$MISSING ca-certificates"
  if [ -n "$MISSING" ]; then
    echo "    缺少:$MISSING -> apt 安装"
    if command -v apt-get >/dev/null 2>&1; then
      SUDO=""; [ "$(id -u)" != "0" ] && SUDO="sudo"
      $SUDO apt-get update -qq
      # shellcheck disable=SC2086
      $SUDO apt-get install -y $APT_PACKAGES
    else
      echo "    [警告] 非 apt 系统, 请手动安装:$MISSING" >&2
    fi
  else
    echo "    系统依赖齐备"
  fi
fi

command -v openssl >/dev/null 2>&1 || {
  echo "错误: 需要 openssl (apt install openssl)" >&2; exit 1; }

# ---- Python 依赖装在项目内的 .venv, 不污染系统 ----
cd "$SRC"
if [ ! -x "$SRC/.venv/bin/python" ]; then
  echo "==> 创建 .venv ($PY)"
  "$PY" -m venv .venv
fi
VPY="$SRC/.venv/bin/python"
[ -x "$VPY" ] || VPY="$PY"

if ! "$VPY" -c "import PyInstaller" 2>/dev/null; then
  echo "==> 安装 PyInstaller"
  "$VPY" -m pip install -q --upgrade pip
  "$VPY" -m pip install -q pyinstaller
fi

echo "==> 冻结 (--onefile)"
mkdir -p "$OUT"
"$VPY" -m PyInstaller --onefile --name hublane \
  --distpath "$OUT" --workpath "$OUT/build" --specpath "$OUT" --noconfirm \
  hublane.py || exit 1

EXE="$OUT/hublane"
[ -x "$EXE" ] || EXE="$OUT/hublane.exe"
[ -f "$EXE" ] || { echo "错误: 未产出可执行文件" >&2; exit 1; }
echo "==> 产物: $EXE ($(du -h "$EXE" | cut -f1))"

if [ "$SMOKE" = 1 ]; then
  echo "==> 冒烟验证"
  # 冻结形态的 INSTALL_DIR 必须落在稳定目录, 否则证书每次启动都丢
  SMOKE_HOME="$(mktemp -d)"
  echo "    HUBLANE_HOME=$SMOKE_HOME"
  "$EXE" --version || exit 1
  HUBLANE_HOME="$SMOKE_HOME" "$EXE" --check \
    || { echo "    --check 失败(未指定 --config 时用 \$HUBLANE_HOME/config.json)" >&2; exit 1; }
  HUBLANE_HOME="$SMOKE_HOME" "$EXE" --renew-certs || exit 1
  for f in ca.crt ca.key server.crt server.key; do
    [ -f "$SMOKE_HOME/$f" ] || { echo "错误: 证书未落到 $SMOKE_HOME/$f" >&2; exit 1; }
  done
  echo "    证书已落到稳定目录 ✓"
  openssl verify -CAfile "$SMOKE_HOME/ca.crt" "$SMOKE_HOME/server.crt" \
    || { echo "错误: 证书链自校验失败" >&2; exit 1; }
  echo "    证书链自校验通过 ✓"
  rm -rf "$SMOKE_HOME"
fi

echo "完成。"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) ;;
  *) echo "注意: 本次只验证了 $(uname -s) 产物; Windows exe 仍需在 Windows 上跑一遍。" ;;
esac