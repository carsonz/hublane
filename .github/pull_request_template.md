## 变更内容

<!-- 一句话说明这个 PR 做了什么 -->

## 动机 / 关联 issue

<!-- 为什么需要这个改动 -->

## 自检清单

- [ ] 未引入第三方运行时依赖（`hublane.py` 仍只用标准库）
- [ ] `python -m flake8 hublane.py tests tools` 通过
- [ ] `python hublane.py --check` 通过
- [ ] `python -m unittest discover -s tests -v` 全绿（新增行为已补测试）
- [ ] 行为变更已同步 README.md / README.zh-CN.md / ROADMAP.md / CHANGELOG.md
- [ ] 未提交任何证书或私钥（`ca.key` / `server.key` / `*.crt`）

## 测试说明

<!-- 怎么验证的: 命令、观察到的结果(可附 /status 或面板截图) -->
