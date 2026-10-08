#!/usr/bin/env python
"""用 PyInstaller 把 hublane 打包成 Windows 单文件 exe。

用法::

    python tools/build-exe.py                # 打包 (缺证书会自动生成)
    python tools/build-exe.py --one-dir      # 目录模式, 便于排查杀软误报
    python tools/build-exe.py --skip-certs   # 不预生成证书(运行时需自备 openssl)

产物: dist/hublane.exe

配套: 打 **Linux/WSL** 产物并用 apt 补系统依赖、跑冻结冒烟, 用
``tools/build_exe.sh``(本脚本只面向 Windows)。

为什么需要这个脚本
------------------
hublane.py 是零第三方依赖的单文件程序, 但目标用户未必装了 Python,
所以 ROADMAP 里把"Windows 免 Python 单文件 exe"(v0.2.0)列为待办。

三个必须处理的坑(EVALUATIONS.md 里列的风险, 这里逐条落地):

1. **数据目录**: onefile 模式下 ``__file__`` 指向 ``_MEIPASS`` 临时目录,
   每次运行都被清空。hublane.py 里的 ``INSTALL_DIR`` 已改为冻结时取
   ``sys.executable`` 所在目录, 本脚本用 ``--add-data`` 把 config/证书
   打进包内, 运行时由 ``seed_bundled()`` 首次播种到 exe 旁边。
2. **证书**: 目标机可能没有 openssl(Windows 默认没有)。因此在**构建时**
   用本机的 openssl 预生成一套 CA + 叶子证书打进包, 用户开箱即用。
   代价: exe 内含 CA 私钥 —— 它只应分发给最终用户本人, 且只装进自己的
   信任库。若要分发给他人, 请用 ``--skip-certs`` 并让对方自行
   ``hublane.exe --renew-ca``(需要 openssl)。
3. **杀软误报**: PyInstaller 产物是已知的高误报形态。本脚本不处理签名
   (需要付费证书), 但会在结束时提示, 并说明 ``--onedir`` 更不容易被拦。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

# Windows 控制台默认不是 UTF-8, 直接 print 中文会乱码
if getattr(sys.stdout, "encoding", "") and \
        sys.stdout.encoding.lower().replace("-", "") not in ("utf8",):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "hublane.py")
CERT_FILES = ("ca.crt", "ca.key", "server.crt", "server.key")


def find_openssl():
    """与 hublane.py find_openssl() 保持一致"""
    candidates = ["openssl",
                  r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
                  r"C:\Program Files\OpenSSL-Win32\bin\openssl.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\usr\bin\openssl.exe"),
                  r"C:\Program Files\Git\usr\bin\openssl.exe",
                  r"C:\Program Files (x86)\Git\usr\bin\openssl.exe"]
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
        if c != "openssl" and os.path.exists(c):
            return c
    return None


def make_certs(workdir):
    """构建期生成 CA + 叶子证书, 返回 workdir 路径列表(供 --add-data)。

    复用 hublane.gen_certs(), 保证与运行时 --renew-certs 产出同构。
    """
    sys.path.insert(0, ROOT)
    import hublane as H

    saved = {name: getattr(H, name) for name in
             ("INSTALL_DIR", "CERT", "KEY", "CA_CRT", "CA_KEY", "CSR")}
    H.INSTALL_DIR = workdir
    H.CERT = os.path.join(workdir, "server.crt")
    H.KEY = os.path.join(workdir, "server.key")
    H.CA_CRT = os.path.join(workdir, "ca.crt")
    H.CA_KEY = os.path.join(workdir, "ca.key")
    H.CSR = os.path.join(workdir, "server.csr")
    try:
        ok, msg = H.gen_certs(renew_ca=True)
        if not ok:
            return None, msg
        missing = [f for f in CERT_FILES if not os.path.exists(os.path.join(workdir, f))]
        if missing:
            return None, "证书生成不完整: 缺少 %s" % ", ".join(missing)
        return workdir, msg
    finally:
        for name, value in saved.items():
            setattr(H, name, value)


def main(argv=None):
    ap = argparse.ArgumentParser(description="打包 hublane.exe")
    ap.add_argument("--one-dir", action="store_true",
                    help="目录模式(--onedir), 启动更快且更少被杀软误报")
    ap.add_argument("--skip-certs", action="store_true",
                    help="不预生成证书(目标机需自行准备 openssl 与证书)")
    ap.add_argument("--python", default=sys.executable,
                    help="用哪个解释器跑 PyInstaller (默认当前)")
    args = ap.parse_args(argv)

    if os.name != "nt":
        print("[跳过] exe 打包只在 Windows 上进行", file=sys.stderr)
        return 0

    if not os.path.exists(SRC):
        print("[错误] 找不到 %s" % SRC, file=sys.stderr)
        return 1

    probe = subprocess.run([args.python, "-c", "import PyInstaller"],
                           capture_output=True)
    if probe.returncode != 0:
        print("[错误] 当前解释器没有 PyInstaller:", args.python)
        print("       pip install pyinstaller", file=sys.stderr)
        return 1

    stage = tempfile.mkdtemp(prefix="hublane-build-")
    data = []
    try:
        # config.json 必须进包: 首次运行时 seed_bundled() 会播种到 exe 旁边
        shutil.copy2(os.path.join(ROOT, "config.json"),
                     os.path.join(stage, "config.json"))
        data.append(("config.json", "."))

        if args.skip_certs:
            print("[1/3] --skip-certs: 不预生成证书")
            print("      目标机首次运行前需自行执行 hublane.exe --renew-ca")
        else:
            exe = find_openssl()
            if not exe:
                print("[错误] 未找到 openssl, 无法预生成证书。", file=sys.stderr)
                print("       请先运行 tools/setup-windows-env.ps1 安装 openssl,",
                      file=sys.stderr)
                print("       或加 --skip-certs 跳过。", file=sys.stderr)
                return 1
            print("[1/3] 预生成证书 (openssl: %s)" % exe)
            _, msg = make_certs(stage)
            print("      " + msg)
            for name in CERT_FILES:
                path = os.path.join(stage, name)
                if os.path.exists(path):
                    data.append((name, "."))

        # --add-data "src;dest" 是 Windows 形式(PyInstaller 6 用 ; 分隔)
        add_data = []
        for src, dest in data:
            add_data += ["--add-data", "%s;%s" % (os.path.join(stage, src), dest)]

        cmd = [args.python, "-m", "PyInstaller",
               "--noconfirm", "--clean",
               "--name", "hublane",
               "--distpath", os.path.join(ROOT, "dist"),
               "--workpath", os.path.join(ROOT, "build"),
               "--specpath", os.path.join(ROOT, "build"),
               "--console",
               # hublane 只用标准库, 无 hiddenimport 可挖; 显式声明让意图清晰
               "--hidden-import", "http.server",
               "--hidden-import", "concurrent.futures"]
        cmd += add_data
        cmd += ["--onedir" if args.one_dir else "--onefile", SRC]

        print("[2/3] PyInstaller: %s" % (" ".join(cmd[2:])))
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            print("[错误] PyInstaller 失败 (exit=%d)" % result.returncode,
                  file=sys.stderr)
            return result.returncode

        out = os.path.join(ROOT, "dist", "hublane",
                           "hublane.exe") if args.one_dir else \
            os.path.join(ROOT, "dist", "hublane.exe")
        print("[3/3] 完成" if os.path.exists(out) else "[3/3] 未找到产物!")
        if os.path.exists(out):
            print("      %s (%.1f MB)" % (out, os.path.getsize(out) / 1048576.0))
            print("")
            print("      自测:  %s --version" % out)
            print("            %s --check" % out)
            print("      安装信任: certutil -addstore -user -f Root <exe目录>\\ca.crt")
            print("")
            print("      注意: 未做代码签名, SmartScreen/杀软可能拦截。")
            print("            可加 --one-dir 降低误报, 或用 --skip-certs 重新构建。")
        return 0 if os.path.exists(out) else 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
