#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 CHANGELOG.md 抽出某个版本的段落, 用作 Release 说明

用法:  python3 tools/changelog_section.py v0.1.0 > RELEASE_NOTES.md
       python3 tools/changelog_section.py 0.1.0
找不到该版本时, 退化为 "## [Unreleased]" 段落; 再找不到则输出占位说明。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
HEADING = re.compile(r"^##\s+\[(?P<name>[^\]]+)\]")


def sections(text):
    """把 CHANGELOG 切成 {版本名: 正文}"""
    out = {}
    name = None
    buf = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            if name is not None:
                out[name] = "\n".join(buf).strip()
            name = match.group("name")
            buf = []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf).strip()
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    wanted = (argv[0] if argv else "").strip().lstrip("v")
    try:
        with open(CHANGELOG, encoding="utf-8") as fh:
            data = sections(fh.read())
    except OSError as exc:
        print("无法读取 CHANGELOG.md: %s" % exc, file=sys.stderr)
        return 1
    body = data.get(wanted) or data.get("Unreleased")
    if body is None:
        body = "详见 CHANGELOG.md"
    if wanted and wanted not in data:
        print("> 未在 CHANGELOG.md 找到 [%s], 以下为 [Unreleased] 段落。\n" % wanted)
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
