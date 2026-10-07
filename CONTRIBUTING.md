# Contributing to hublane

Thanks for your interest in improving hublane!

## Project philosophy

- **Zero runtime dependencies.** hublane runs on the Python standard library
  only. Do not introduce third-party imports into `hublane.py`.
- **Single-file core.** The relay logic lives in `hublane.py`. Keep it that
  way unless there is a strong reason to split.
- **Cross-platform.** Must work on WSL/Linux and Windows (Python 3.9+).

## Development setup

```bash
# 1. Clone
git clone https://github.com/carsonz/hublane.git
cd hublane

# 2. (Optional) create a venv — not strictly required, stdlib only
python3 -m venv .venv && source .venv/bin/activate

# 3. Run the test suite (unit + integration)
python3 -m unittest discover -s tests -v
# or, with make (WSL/Linux):
make test
```

Integration tests spin up fake upstreams locally (a plaintext mirror and a
TLS "Watt" origin) and drive a real TLS client through the proxy — **no internet
access is needed**. They require `openssl` on `PATH` to generate a throwaway CA;
without it the test module is skipped automatically.

## Code style

- `flake8` must pass (enforced in CI, config in `setup.cfg`:
  `max-line-length = 100`, `max-complexity = 15`).
- 4-space indentation, explicit over clever.
- Add/extend tests for any behavior change:
  - pure logic → `tests/test_hublane.py`
  - anything touching sockets/upstreams → `tests/test_integration.py`

```bash
make lint     # python3 -m flake8 hublane.py tests tools bench
make check    # 配置校验 + 版本一致性
make bench    # 本地压测, 与 bench/baseline.json 对比(不需要外网)
make cov      # 覆盖率(需 pip install coverage)
```

新增"会被接管的站点"（分组库 / `BUILTIN_PER_HOST_UPSTREAMS`）时，叶证书 SAN 会由
`leaf_san()` 自动覆盖，`install.sh` 与 `install-windows.bat` 里的 SAN 常量由
`TestCertSan` 校验必须一致 —— 改这三处任一处都要同步另外两处。

架构说明见 `docs/ARCHITECTURE.md`，镜像/站点库的实测记录见 `docs/MIRRORS.md`，
`[A]` 待评估项的结论见 `docs/EVALUATIONS.md`。

## Certificates

Certificates are generated locally by the install scripts via `openssl`.
Never commit `ca.key`, `server.key`, `ca.crt`, or `server.crt`.

## Submitting changes

1. Fork and create a feature branch.
2. Make your change with tests.
3. Run `python3 hublane.py --check` to validate config handling.
4. Keep docs in sync: `README.md`, `README.zh-CN.md`, `ROADMAP.md`,
   `CHANGELOG.md` (the PR template has a checklist).
5. Open a pull request describing the motivation and the change.

## Releasing

Releases are tag-driven and automated (`.github/workflows/release.yml`):

1. Bump the version in **both** `hublane.py` (`VERSION`) and
   `pyproject.toml` (`version`) — CI checks they match with
   `python3 tools/check_version.py`.
2. Move the `[Unreleased]` notes in `CHANGELOG.md` under the new version
   heading (`## [0.1.0] - YYYY-MM-DD`).
3. Commit, then push a tag: `git tag v0.1.0 && git push origin v0.1.0`.
4. The workflow re-runs lint + tests, packages a source `tar.gz` plus
   sdist/wheel, writes `SHA256SUMS`, and creates the GitHub Release using the
   matching CHANGELOG section as the release body.

Preview locally before tagging:

```bash
make versions                       # 版本号是否一致
make notes                          # Release 说明草稿(取自 CHANGELOG 的 Unreleased)
make package                        # 本地打包 + SHA256SUMS
```
