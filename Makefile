# hublane 常用开发命令 (WSL/Linux; Windows 见 CONTRIBUTING.md 里的等价命令)
PY ?= python3

.PHONY: help test lint bench cov check run panel versions notes package clean

help:  ## 显示可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

test:  ## 单元 + 集成测试
	$(PY) -m unittest discover -s tests -v

lint:  ## flake8 风格检查 (配置见 setup.cfg)
	$(PY) -m flake8 hublane.py tests tools bench

bench:  ## 本地压测(与 bench/baseline.json 对比, 不需要外网)
	$(PY) bench/bench_relay.py --check bench/baseline.json

bench-save:  ## 重新采集压测基线
	$(PY) bench/bench_relay.py --save bench/baseline.json

cov:  ## 覆盖率报告(需要 pip install coverage)
	$(PY) -m coverage run -m unittest discover -s tests
	$(PY) -m coverage report -m --include=hublane.py

check:  ## 配置校验 + 版本一致性
	$(PY) hublane.py --check
	$(PY) tools/check_version.py

run:  ## 前台运行(读当前目录 config.json)
	$(PY) hublane.py --config config.json

panel:  ## 打开本地指标面板
	@echo "http://127.0.0.1:28898/"
	@curl -fsS http://127.0.0.1:28898/healthz || echo "面板未启动"

versions:  ## 打印当前版本号
	@$(PY) tools/check_version.py

notes:  ## 生成下一个版本的 Release 说明草稿
	@$(PY) tools/changelog_section.py Unreleased

package:  ## 本地打包(源码 tar.gz + sdist + wheel + SHA256SUMS)
	@VER=$$($(PY) -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('hublane.py').read()).group(1))"); \
	mkdir -p dist; \
	git archive --format=tar.gz --prefix="hublane-$$VER/" -o "dist/hublane-$$VER.tar.gz" HEAD; \
	if $(PY) -c "import build" 2>/dev/null; then \
		$(PY) -m build --sdist --wheel --outdir dist; \
	else \
		echo "跳过 sdist/wheel: 未安装 build (pip install build)"; \
	fi; \
	cd dist && sha256sum ./* > SHA256SUMS && cat SHA256SUMS

clean:  ## 清理构建产物
	rm -rf dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
