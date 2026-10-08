#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布门禁: 版本号只允许有一处来源 —— hublane.py 里的 VERSION

历史做法是在 pyproject.toml 里也写一份、再比对两处是否相同, 本质上仍是
"多处设置 + 事后检查", 漏改就会构建出一个版本号错误的产物。
现在 pyproject.toml 用 setuptools 的 dynamic 在构建期读取 hublane.VERSION,
所以这个脚本的职责从"比对"变成"守住单一来源":

  1. hublane.py 必须能取到 VERSION;
  2. pyproject.toml **不得**再硬编码 version(只允许 dynamic);
  3. pyproject.toml 的 dynamic 必须确实指向 hublane.VERSION;
  4. 若传入 tag, tag 必须与 hublane.VERSION 一致。

用法:  python3 tools/check_version.py v0.1.1     # CI 传 tag
       python3 tools/check_version.py            # 只校验单一来源
退出码: 0 通过, 1 不通过(并打印原因)
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def module_version():
    """单一来源: hublane.py 里的 VERSION = \"x.y.z\""""
    with io.open(os.path.join(ROOT, "hublane.py"), encoding="utf-8") as fh:
        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.MULTILINE)
    return match.group(1) if match else None


def read_pyproject():
    with io.open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        return fh.read()


def hardcoded_pyproject_version(text):
    """在 [project] 段里找写死的 version = "..."; dynamic 声明不算。"""
    # 只认 [project] 表下的顶层 version =, 避免把 [tool.*] 里的误判进来
    match = re.search(r'^\[project\]\s*$(.*?)(?=^\[)', text,
                      re.MULTILINE | re.DOTALL)
    section = match.group(1) if match else text
    hit = re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)
    return hit.group(1) if hit else None


def dynamic_attr(text):
    """[tool.setuptools.dynamic] 里的 version = { attr = "..." }"""
    match = re.search(r'^\[tool\.setuptools\.dynamic\]\s*$(.*?)(?=^\[|\Z)', text,
                      re.MULTILINE | re.DOTALL)
    if not match:
        return None
    hit = re.search(r'version\s*=\s*\{\s*attr\s*=\s*"([^"]+)"', match.group(1))
    return hit.group(1) if hit else None


def ensure_utf8_console():
    """Windows 英文 locale 下 stdout 是 cp1252, 而本脚本输出中文 ——
    直接 print 会 UnicodeEncodeError 让整个 CI 任务失败(实测于 windows-latest)。
    见 hublane.ensure_utf8_console() 里的完整说明。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    ensure_utf8_console()
    argv = sys.argv[1:] if argv is None else argv
    ref = (argv[0] if argv else "").strip()
    tag = ref.lstrip("v") if ref else None
    module = module_version()
    text = read_pyproject()
    hardcoded = hardcoded_pyproject_version(text)
    attr = dynamic_attr(text)
    problems = []

    if not module:
        problems.append("hublane.py 里找不到 VERSION(版本号唯一来源)")
    if hardcoded:
        problems.append(
            "pyproject.toml 里写死了 version=%s —— 版本号只能有一处来源; "
            "请删掉它, 改用 [tool.setuptools.dynamic] version = { attr = "
            "\"hublane.VERSION\" }" % hardcoded)
    if not attr:
        problems.append("pyproject.toml 缺少 [tool.setuptools.dynamic] version")
    elif attr != "hublane.VERSION":
        problems.append("pyproject.toml 的 dynamic version 指向 %r, 应为 "
                        "hublane.VERSION" % attr)
    if tag and module and tag != module:
        problems.append("tag %s 与 hublane.py VERSION=%s 不一致" % (ref, module))

    if problems:
        for item in problems:
            print("版本检查失败: %s" % item, file=sys.stderr)
        return 1
    print("版本一致: %s%s(单一来源: hublane.py)"
          % (module, " (tag %s)" % ref if ref else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
