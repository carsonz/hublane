#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布门禁: 校验 tag / hublane.VERSION / pyproject.version 三者一致

用法:  python3 tools/check_version.py v0.1.0
       python3 tools/check_version.py            # 只比对 hublane.py 与 pyproject
退出码: 0 一致, 1 不一致(并打印差异)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def module_version():
    with open(os.path.join(ROOT, "hublane.py"), encoding="utf-8") as fh:
        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.MULTILINE)
    return match.group(1) if match else None


def pyproject_version():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        match = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.MULTILINE)
    return match.group(1) if match else None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ref = (argv[0] if argv else "").strip()
    tag = ref.lstrip("v") if ref else None
    module = module_version()
    project = pyproject_version()
    problems = []
    if not module:
        problems.append("hublane.py 里找不到 VERSION")
    if not project:
        problems.append("pyproject.toml 里找不到 version")
    if module and project and module != project:
        problems.append("hublane.py VERSION=%s 与 pyproject.toml version=%s 不一致"
                        % (module, project))
    if tag and module and tag != module:
        problems.append("tag %s 与 hublane.py VERSION=%s 不一致" % (ref, module))
    if problems:
        for item in problems:
            print("版本不一致: %s" % item, file=sys.stderr)
        return 1
    print("版本一致: %s%s" % (module, " (tag %s)" % ref if ref else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
