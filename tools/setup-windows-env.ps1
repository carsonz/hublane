<#
.SYNOPSIS
    hublane Windows 环境引导: 用 winget 补齐缺失的系统软件, 并建立 .venv。

.DESCRIPTION
    hublane 本体零第三方依赖, 但在 Windows 上"跑起来"依赖三样系统软件:
      1. winget                     —— 本脚本自身的安装通道
      2. Python >= 3.9              —— 运行 hublane.py (pyproject: requires-python)
      3. openssl                    —— 生成/续期本地 CA 与叶子证书
                                       (Windows 通常没有系统 openssl,
                                        现由 Git for Windows 自带那份兜底)
    本脚本按"缺什么装什么"的顺序检测并补齐, 全部安装动作都走 winget,
    已存在的组件不会重复安装 (除非 -Force)。

.PARAMETER PythonVersion
    需要的 Python 次版本, 默认 3.12 (与 README / CI 一致)。

.PARAMETER Force
    忽略"已安装"检测, 强制重新安装全部组件。

.PARAMETER SkipVenv
    不创建 .venv 目录。

.PARAMETER NoDevDeps
    不往 .venv 里装 dev 依赖 (flake8/build/coverage/pyinstaller)。

.PARAMETER SkipOpenSSL
    跳过 openssl 检测与安装。

.EXAMPLE
    pwsh -File tools/setup-windows-env.ps1
    pwsh -File tools/setup-windows-env.ps1 -PythonVersion 3.13 -Force
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [switch]$Force,
    [switch]$SkipVenv,
    [switch]$NoDevDeps,
    [switch]$SkipOpenSSL
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvDir = Join-Path $RepoRoot ".venv"

# winget 包 ID (已核实存在于 winget 源)
$WingetPythonManager = "Python.PythonInstallManager"
$WingetOpenSSL       = "ShiningLight.OpenSSL.LTS.Light"   # 3.5.x, 与 Git 自带版本一致
$WingetGit           = "Git.Git"                          # 自带 openssl, 作为兜底

# 与 hublane.py find_openssl() 保持一致的候选位置 (含 winget 安装的 OpenSSL)
$OpenSslCandidates = @(
    "openssl",
    "$env:ProgramFiles\OpenSSL-Win64\bin\openssl.exe",
    "$env:ProgramFiles\OpenSSL-Win32\bin\openssl.exe",
    "${env:ProgramFiles(x86)}\OpenSSL-Win32\bin\openssl.exe",
    "$env:LOCALAPPDATA\Programs\Git\usr\bin\openssl.exe",
    "$env:ProgramFiles\Git\usr\bin\openssl.exe",
    "${env:ProgramFiles(x86)}\Git\usr\bin\openssl.exe"
)

$script:Failures = @()

function Write-Step  { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "    [OK]   $m" -ForegroundColor Green }
function Write-Warn  { param($m) Write-Host "    [WARN] $m" -ForegroundColor Yellow }
function Write-Fail  { param($m) Write-Host "    [FAIL] $m" -ForegroundColor Red }
function Write-Info  { param($m) Write-Host "    $m" -ForegroundColor DarkGray }

function Update-SessionPath {
    # winget 装完软件后, 当前会话的 $env:Path 不会自动刷新, 这里从注册表重建
    try {
        $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $user    = [Environment]::GetEnvironmentVariable("Path", "User")
        $parts = @()
        if ($machine) { $parts += $machine }
        if ($user)    { $parts += $user }
        if ($parts)   { $env:Path = ($parts -join ";") }
    } catch {
        Write-Warn "刷新 PATH 失败: $($_.Exception.Message)"
    }
}

function Invoke-WingetInstall {
    param(
        [Parameter(Mandatory)][string]$Id,
        [string]$FriendlyName = $Id,
        [string[]]$WingetArgs = @()
    )
    Write-Step "安装 $FriendlyName ($Id)"
    $argv = @("install", "--id", $Id, "-e", "--accept-package-agreements",
              "--accept-source-agreements", "--disable-interactivity")
    if ($WingetArgs) { $argv += $WingetArgs }
    & winget @argv
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "winget 安装 $Id 失败 (exit=$LASTEXITCODE)"
        return $false
    }
    Update-SessionPath
    Write-Ok "$FriendlyName 安装完成"
    return $true
}

function Resolve-PythonExe {
    # 返回一个满足 >=3.9 的 python 解释器路径; 找不到返回 $null
    $candidates = New-Object System.Collections.Generic.List[string]

    # 1) py 启动器 (最可靠, 能选版本)
    $pyExe = (Get-Command py -ErrorAction SilentlyContinue)
    if ($pyExe) {
        foreach ($tag in @("-3.$PythonVersion", "-3")) {
            $found = & $pyExe.Source $tag -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $candidates.Add($found.Trim()) }
        }
    }
    # 2) PATH 上的 python
    $pyCmd = (Get-Command python -ErrorAction SilentlyContinue)
    if ($pyCmd) { $candidates.Add($pyCmd.Source) }
    # 3) python.org 新版安装器 (Python Install Manager) 的托管目录
    $coreRoot = Join-Path $env:LOCALAPPDATA "Python"
    if (Test-Path $coreRoot) {
        Get-ChildItem -Path $coreRoot -Directory -Filter "pythoncore-$PythonVersion*" -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates.Add((Join-Path $_.FullName "python.exe")) }
    }

    foreach ($c in $candidates) {
        if (-not $c) { continue }
        if (-not (Test-Path $c)) { continue }
        $ok = & $c -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    return $null
}

function Resolve-OpenSsl {
    foreach ($c in $OpenSslCandidates) {
        if (-not $c) { continue }
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        if ($c -ne "openssl" -and (Test-Path $c)) { return $c }
    }
    return $null
}

# ---------------------------------------------------------------- winget
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  hublane Windows 环境引导 (winget)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

$winget = Get-Command winget -ErrorAction SilentlyContinue
if (-not $winget) {
    Write-Fail "未找到 winget。"
    Write-Info "请先安装 '应用安装程序'(App Installer):"
    Write-Info "  winget 本身缺失时无法自动补齐, 请从 Microsoft Store 安装"
    Write-Info "  '应用安装程序', 或下载 https://aka.ms/getwinget"
    exit 1
}
Write-Ok "winget $((& winget --version).Trim())"

# ------------------------------------------------- 1) Python Install Manager
if ($Force) {
    Invoke-WingetInstall -Id $WingetPythonManager -FriendlyName "Python Install Manager (py 管理器)" | Out-Null
} else {
    Write-Step "检查 Python Install Manager"
    $hasPyLauncher = [bool](Get-Command py -ErrorAction SilentlyContinue)
    if ($hasPyLauncher) {
        Write-Ok "已具备 Python 版本管理能力 (py 启动器在位)"
    } else {
        Write-Info "未检测到 py 启动器, 安装 Python Install Manager ..."
        if (Invoke-WingetInstall -Id $WingetPythonManager -FriendlyName "Python Install Manager") {
            Write-Ok "已安装 Python Install Manager"
        }
    }
}

# ------------------------------------------------------------- 2) Python
Write-Step "检查 Python >= 3.9 (目标 $PythonVersion)"
$pythonExe = Resolve-PythonExe
if ($pythonExe -and -not $Force) {
    $ver = (& $pythonExe -V 2>&1) -join ""
    Write-Ok "Python $ver -> $pythonExe"
} else {
    $wingetPyId = "Python.Python.$PythonVersion"
    Write-Info "未找到满足条件的 Python, 尝试 winget 安装 $wingetPyId"
    if (-not (Invoke-WingetInstall -Id $wingetPyId -FriendlyName "Python $PythonVersion" -WingetArgs @("--scope", "user"))) {
        Write-Warn "winget 安装失败, 尝试不带 --scope user 重试"
        Invoke-WingetInstall -Id $wingetPyId -FriendlyName "Python $PythonVersion" | Out-Null
    }
    Update-SessionPath
    $pythonExe = Resolve-PythonExe
    if (-not $pythonExe) {
        Write-Fail "仍找不到可用的 Python >= 3.9"
        $script:Failures += "Python"
    } else {
        $ver = (& $pythonExe -V 2>&1) -join ""
        Write-Ok "Python $ver -> $pythonExe"
    }
}

# ------------------------------------------------------------ 3) OpenSSL
if ($SkipOpenSSL) {
    Write-Step "跳过 openssl (已指定 -SkipOpenSSL)"
} else {
    Write-Step "检查 openssl (证书生成依赖)"
    $sslExe = Resolve-OpenSsl
    if ($sslExe -and -not $Force) {
        $osslVer = (& $sslExe version 2>&1) -join ""
        Write-Ok "openssl  $osslVer"
        Write-Info "  $sslExe"
    } else {
        Write-Info "未找到 openssl, 尝试 winget 安装 $WingetOpenSSL"
        if (Invoke-WingetInstall -Id $WingetOpenSSL -FriendlyName "OpenSSL (ShiningLight LTS)") {
            Update-SessionPath
            $sslExe = Resolve-OpenSsl
        }
        if (-not $sslExe) {
            Write-Warn "OpenSSL 未能就绪, 回退安装 Git for Windows (自带 openssl)"
            if (Invoke-WingetInstall -Id $WingetGit -FriendlyName "Git for Windows") {
                $sslExe = Resolve-OpenSsl
            }
        }
        if ($sslExe) {
            $osslVer = (& $sslExe version 2>&1) -join ""
            Write-Ok "openssl  $osslVer"
            Write-Info "  $sslExe"
        } else {
            Write-Fail "openssl 不可用 —— hublane 无法生成/续期证书 (--renew-certs 会失败)"
            $script:Failures += "OpenSSL"
        }
    }
}

# ---------------------------------------------------------------- 4) .venv
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if ($SkipVenv) {
    Write-Step "跳过 .venv (已指定 -SkipVenv)"
} elseif ((Test-Path $venvPython) -and -not $Force) {
    Write-Ok ".venv 已存在: $VenvDir"
} elseif (-not $pythonExe) {
    Write-Warn "没有可用 Python, 无法创建 .venv"
    $script:Failures += ".venv"
} else {
    Write-Step "创建 .venv ($VenvDir)"
    & $pythonExe -m venv $VenvDir
    if ($LASTEXITCODE -eq 0 -and (Test-Path $venvPython)) {
        Write-Ok ".venv 创建完成"
    } else {
        Write-Fail ".venv 创建失败"
        $script:Failures += ".venv"
    }
}

# --------------------------------------------------------- 5) dev 依赖
if ($NoDevDeps) {
    Write-Step "跳过 dev 依赖 (已指定 -NoDevDeps)"
} elseif (Test-Path $venvPython) {
    Write-Step "安装 dev / 打包依赖到 .venv"
    & $venvPython -m pip install --upgrade pip --quiet
    # editable 安装会把 hublane 注册进 .venv, 顺带验证 pyproject 可用
    & $venvPython -m pip install -e "$RepoRoot[dev]" --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "editable 安装失败 —— pyproject.toml / build 后端有问题"
        $script:Failures += "pyproject(可安装性)"
    } else {
        Write-Ok "editable 安装完成 (pyproject.toml 可用)"
    }
    # pyinstaller: 打包 hublane.exe 用 (tools/build-exe.py)
    & $venvPython -m pip install pyinstaller --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "pyinstaller 安装完成"
    } else {
        Write-Warn "pyinstaller 安装失败 (网络?), 打包 exe 会不可用"
    }
} else {
    Write-Warn "无 .venv, 跳过依赖安装"
}

# ---------------------------------------------------------------- 汇总
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  验证摘要" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

$checkPython = if (Test-Path $venvPython) { $venvPython } else { $pythonExe }
if ($checkPython) {
    $pv = (& $checkPython -V 2>&1) -join ""
    Write-Host "  Python    : $pv" -ForegroundColor Green
} else {
    Write-Host "  Python    : 缺失" -ForegroundColor Red
}
$sslFinal = Resolve-OpenSsl
if ($sslFinal) {
    Write-Host "  OpenSSL   : $sslFinal" -ForegroundColor Green
} else {
    Write-Host "  OpenSSL   : 缺失" -ForegroundColor Red
}
Write-Host "  hublane.py: $(Join-Path $RepoRoot 'hublane.py')" -ForegroundColor Green
$venvState = if (Test-Path $VenvDir) { $VenvDir } else { "未创建" }
$venvColor = if (Test-Path $VenvDir) { "Green" } else { "Yellow" }
Write-Host "  .venv     : $venvState" -ForegroundColor $venvColor

Write-Host ""
Write-Host "  下一步:" -ForegroundColor Cyan
if (Test-Path $venvPython) {
    Write-Host "    1) 跑测试    : $venvPython -m unittest discover -s tests -v"
    Write-Host "    2) 配置自检  : $venvPython hublane.py --config config.json --check"
    Write-Host "    3) 打包 exe  : $venvPython tools/build-exe.py"
} else {
    Write-Host "    1) 跑测试    : python -m unittest discover -s tests -v"
}
Write-Host "    4) 装服务    : 右键以管理员身份运行 install-windows-service.bat"
Write-Host ""

if ($script:Failures.Count -gt 0) {
    Write-Host "  未完成项: $($script:Failures -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "  环境就绪。" -ForegroundColor Green
exit 0
