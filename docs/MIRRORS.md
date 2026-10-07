# 镜像库与站点库（含实测记录）

`hublane.py` 里的 `MIRROR_PREFIX`（raw 文件加速站）、`SITE_MIRRORS`（非 GitHub 站点镜像）、
`DOH_ENDPOINTS_DEFAULT`（DoH 端点池）、`extra_host_groups`（境外站点分组）都是"库"，
条目会随时间和网络环境失效。**本文档记录入库依据与复测方法**，改动库时请同步更新。

## 实测（2026-10-07，受限网络环境下）

用 `python3` + 内部 opener 逐条 `Range: bytes=0-128` 探测，结果如下
（FAIL 主要是 TLS 握手超时 / 证书校验失败，即典型的"被中间设备拦"）：

| 官方站点 | 直连 | 镜像 | 镜像状态 |
|---|---|---|---|
| `huggingface.co` | 证书校验失败 | `hf-mirror.com` | 200（`{path}` 路径一致：API、页面、`/resolve/` 均可用） |
| `cdn-lfs.huggingface.co` | 超时 | — | 无路径兼容的镜像（LFS 路径形如 `/repos/xx/yy/...`），只能直连或走 `chain` |
| `pypi.org/simple` | 206 | `pypi.tuna.tsinghua.edu.cn/simple`、`mirrors.aliyun.com/pypi/simple` | 206（路径一致） |
| `files.pythonhosted.org/packages` | 206 | — | 清华未镜像 `/packages`，直连可用 |
| `registry.npmjs.org` | 握手超时 | `registry.npmmirror.com` | 206（路径一致） |
| `proxy.golang.org` | 超时 | `goproxy.cn`、`mirrors.aliyun.com/goproxy` | 206（路径一致） |
| `repo.anaconda.com/pkgs` | 握手超时 | `mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs` | 206（前缀 `/anaconda`） |
| `conda.anaconda.org/{channel}` | — | `…/anaconda/cloud/{channel}` | 206（前缀 `/anaconda/cloud`） |
| `index.crates.io` | 206 | `rsproxy.cn/index` | 206（直连已可用，镜像作备份） |
| `static.crates.io` | 206 | — | 直连可用（`rsproxy` 的下载路径与官方不一致，未入库） |
| `nodejs.org/dist` | 超时 | `registry.npmmirror.com/-/binary/node` | 206（需剥掉 `/dist`，见 `SITE_MIRROR_STRIP`） |
| `cdn.jsdelivr.net/npm/...` | 超时 | `fastly.jsdelivr.net`、`jsd.onmicrosoft.cn` | 206（路径一致） |
| `fonts.googleapis.com` | 证书校验失败 | `fonts.googleapis.cn`、`fonts.loli.net` | 200（**CSS 里返回的字体域名分别是 `fonts.gstatic.cn` / `gstatic.loli.net`**） |
| `fonts.gstatic.com` | 证书校验失败 | `fonts.gstatic.cn`、`gstatic.loli.net` | 206（必须与上面成对使用，否则 CSS 里的字体链接仍被墙） |
| `golang.org` / `go.dev` / `dl.google.com` | 混合 | `golang.google.cn` | 站点可用，但下载路径与官方不同，未入库 |
| `docs.rs`、`kaggle.com`、`download.pytorch.org`、`pypi.org` | 直连可用 | — | 无需镜像 |
| `registry-1.docker.io`、`ghcr.io`、`civitai.com`、`colab`、`zh.wikipedia.org`、`registry.ollama.ai` | 不可达/需鉴权 | — | 这类需要真实出境节点，请配 `chain` |

要点：

1. **字体必须成对替换**：只换 `fonts.googleapis.com` 而不换 `fonts.gstatic.com` 等于没换。
2. **HuggingFace 的坑**：`huggingface.co` 的 API/页面可整站换源，但大文件走
   `cdn-lfs.huggingface.co`，路径不兼容镜像；若你的场景要下大模型，
   更稳的做法是设 `HF_ENDPOINT=https://hf-mirror.com`（客户端层面），
   或把 `cdn-lfs.huggingface.co` 用 `per_host_upstreams` 指向 `chain`。
3. **镜像路径不一致时**：用 `SITE_MIRROR_STRIP` 剥掉官方前缀（如 `/dist`）再拼。

## 复测脚本

```bash
python3 - <<'PY'
import urllib.request
from hublane import _OPENER
for label, url in [
    ("HF 镜像", "https://hf-mirror.com/api/models/bert-base-uncased"),
    ("PyPI 清华", "https://pypi.tuna.tsinghua.edu.cn/simple/requests/"),
    ("npm 淘宝", "https://registry.npmmirror.com/react/latest"),
    ("Go 七牛", "https://goproxy.cn/github.com/gin-gonic/gin/@v/list"),
]:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8",
                                               "Range": "bytes=0-128"})
    try:
        r = _OPENER.open(req, timeout=8)
        print("%-10s %s" % (label, r.status)); r.close()
    except Exception as exc:
        print("%-10s FAIL %s" % (label, repr(exc)[:60]))
PY
```

失效的条目**不必立刻删除**：健康度排序会把连续失败的上游沉到队尾（并计入冷却），
只有当它长期排不上、或域名彻底下线时才从库里移除。

## 新增条目的检查清单

- [ ] 路径是否与官方一致（不一致时用 `SITE_MIRROR_STRIP` 处理，否则别入库）
- [ ] 是否需要在 `BUILTIN_PER_HOST_UPSTREAMS` 里登记（否则该镜像不会自动用于任何域名）
- [ ] 是否加入某个 `extra_host_groups` 分组（决定"是否接管该域名"，默认应关闭）
- [ ] 名字不与 `MIRROR_PREFIX` / `JSDELIVR_HOSTS` 冲突（`TestSiteMirrors`
      `test_no_name_collision_with_raw_mirrors` 会拦截）
- [ ] 更新本文件的实测表格与日期
