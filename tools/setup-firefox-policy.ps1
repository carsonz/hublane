# 把 hublane 的本地 CA 装进 Firefox 自己的证书库(policies.json)。
#
# 背景(实测): Chrome / Edge 用 Windows 证书库, certutil 装完 CA 就能用;
# Firefox 自带证书库, **不读系统信任库** —— 所以装完 CA 之后 Firefox 仍然报
# "MOZILLA_PKIX_ERROR_CA_CERT_USED_AS_END_ENTITY" 之类的证书错误, 只能让用户在
# 设置里手工导入。此前 install-windows.bat 只 echo 一句"请手动导入", 路线图里
# 承诺的 policies.json 从未实现。
#
# 做法: 用 Firefox 官方企业策略 Certificates.Install 直接指定 CA 文件, 再叠加
# ImportEnterpriseRoots(= 路线图里提到的 security.enterprise_roots), 让 Firefox
# 同时信任 Windows 证书库里的 CA。两条都写, 任一生效即可, 互为兜底。
#
# 注意: policies.json 必须放在 firefox.exe 同级的 distribution\ 目录下, 因此
# 写 %ProgramFiles% 需要管理员权限; 没权限时会明确提示, 不会静默失败。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File tools\setup-firefox-policy.ps1
#   ...\setup-firefox-policy.ps1 -CaPath "C:\Users\me\AppData\Local\hublane\ca.crt"
#   ...\setup-firefox-policy.ps1 -DryRun
#
# 退出码: 恒为 0(装不上 Firefox 策略不该让整个安装失败)。

param(
    [string]$CaPath = '',
    # 显式指定 Firefox 安装目录: 正常安装不需要, 供安装器验证(installer smoke test)
    # 与便携版 Firefox 使用。给了它就跳过自动探测。
    [string]$FirefoxDir = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrEmpty($CaPath)) {
    $CaPath = Join-Path $env:LOCALAPPDATA 'hublane\ca.crt'
}
if (-not (Test-Path -LiteralPath $CaPath)) {
    Write-Host "    [提示] 未找到 CA 文件 $CaPath, 跳过 Firefox 策略"
    exit 0
}
$CaPath = (Resolve-Path -LiteralPath $CaPath).Path

# 定位 Firefox 安装目录: 官方默认装到 Program Files / Program Files (x86),
# 自定义路径只能靠注册表卸载键里的 Install Directory 找。
$dirs = New-Object System.Collections.ArrayList
if (-not [string]::IsNullOrEmpty($FirefoxDir)) {
    [void]$dirs.Add($FirefoxDir.TrimEnd('\'))
}
foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if ([string]::IsNullOrEmpty($base)) { continue }
    $d = Join-Path $base 'Mozilla Firefox'
    if ((Test-Path -LiteralPath (Join-Path $d 'firefox.exe')) -and -not $dirs.Contains($d)) {
        [void]$dirs.Add($d)
    }
}
foreach ($key in @('HKLM:\SOFTWARE\Mozilla\Mozilla Firefox',
                   'HKLM:\SOFTWARE\WOW6432Node\Mozilla\Mozilla Firefox')) {
    try {
        foreach ($sub in @(Get-ChildItem -Path $key -ErrorAction Stop)) {
            $main = Get-ItemProperty -Path (Join-Path $sub.PSPath 'Main') -ErrorAction SilentlyContinue
            if (-not $main) { continue }
            $d = ($main.'Install Directory')
            if ([string]::IsNullOrEmpty($d)) { continue }
            $d = $d.TrimEnd('\')
            if ((Test-Path -LiteralPath (Join-Path $d 'firefox.exe')) -and -not $dirs.Contains($d)) {
                [void]$dirs.Add($d)
            }
        }
    } catch { }
}

if ($dirs.Count -eq 0) {
    Write-Host '    [提示] 未检测到 Firefox, 跳过 policies.json'
    exit 0
}

Write-Host '==> 写入 Firefox 企业策略, 让 Firefox 信任 hublane 本地 CA'
foreach ($d in $dirs) {
    $dist = Join-Path $d 'distribution'
    $file = Join-Path $dist 'policies.json'
    Write-Host "    Firefox: $d"

    if ($DryRun) {
        Write-Host "    (DryRun) 将写入 $file"
        continue
    }

    # 已存在就合并, 不覆盖用户/其它软件的既有策略; 解析不了(比如带注释)才备份后重写。
    $obj = $null
    if (Test-Path -LiteralPath $file) {
        try {
            Copy-Item -LiteralPath $file -Destination "$file.bak" -Force
            $obj = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            Write-Host "    [警告] 已有 $file 无法解析为 JSON, 已备份为 policies.json.bak 并重写"
            $obj = $null
        }
    }
    if (-not $obj) { $obj = [pscustomobject]@{} }
    if (-not ($obj.PSObject.Properties.Name -contains 'policies')) {
        $obj | Add-Member -NotePropertyName 'policies' -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $pol = $obj.policies
    if (-not ($pol.PSObject.Properties.Name -contains 'Certificates')) {
        $pol | Add-Member -NotePropertyName 'Certificates' -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $certs = $pol.Certificates
    # ImportEnterpriseRoots: 直接用 Windows 受信任根证书库(对应 security.enterprise_roots)
    $certs | Add-Member -NotePropertyName 'ImportEnterpriseRoots' -NotePropertyValue $true -Force
    $install = @()
    if ($certs.PSObject.Properties.Name -contains 'Install' -and $certs.Install) {
        $install = @($certs.Install)
    }
    if ($install -notcontains $CaPath) { $install += $CaPath }
    $certs | Add-Member -NotePropertyName 'Install' -NotePropertyValue $install -Force

    try {
        if (-not (Test-Path -LiteralPath $dist)) {
            New-Item -ItemType Directory -Path $dist -Force | Out-Null
        }
        $json = $obj | ConvertTo-Json -Depth 8
        $enc = New-Object System.Text.UTF8Encoding($false)   # 无 BOM: Firefox 的 JSON 解析不接受 BOM
        [System.IO.File]::WriteAllText($file, $json + "`r`n", $enc)
        Write-Host "      已写入 $file"
    } catch {
        Write-Host "      [警告] 写入失败: $($_.Exception.Message)"
        Write-Host "             写入 $dist 需要管理员权限; 请以管理员身份重跑安装脚本"
        Write-Host "             或手动导入: Firefox 设置 > 隐私与安全 > 查看证书 > 证书机构 > 导入 $CaPath"
    }
}
Write-Host '    重启 Firefox 后生效(策略只在启动时读取)'
exit 0
