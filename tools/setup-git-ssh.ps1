# 为 Git 配置 GitHub 的 SSH 443 入口, 绕开 DNS 劫持与 22 端口封锁。
#
# 背景(实测): 某些环境(例如 Windows 侧的代理/加速工具)会把 github.com 的 DNS
# 指向 127.0.0.1, 于是 `ssh git@github.com` 连到的是**本机 sshd**, 必然被拒,
# 表现为 "Permission denied (publickey)" —— 看起来像密钥没配好, 其实请求根本没出
# 本机(ssh -v 里能看到 `Connecting to github.com [127.0.0.1]`)。
# 另外受限网络常封 22 端口。GitHub 官方提供 ssh.github.com:443 作为 SSH 入口,
# 既绕开被劫持的域名, 又走通常放行的 443, 是官方支持的做法。
#
# 策略:
#   - 默认 auto: 只在检测到异常(DNS 被劫持 / 22 端口不通)时才写入;
#   - 只**追加** ~/.ssh/config 里一段带标记的配置, 不动你已有的任何内容;
#   - 标记存在就跳过, 重复执行无副作用(幂等)。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File tools\setup-git-ssh.ps1
#   ...\setup-git-ssh.ps1 -Mode always    # 不看检测结果, 直接写
#   ...\setup-git-ssh.ps1 -Mode never     # 只检测, 不写
#   ...\setup-git-ssh.ps1 -DryRun
#
# 回滚: 删掉 ~/.ssh/config 里两个 hublane 标记之间的内容即可。

param(
    [ValidateSet('auto', 'always', 'never')]
    [string]$Mode = 'auto',
    [string]$HomeDir = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Resolve-First([string]$name) {
    try {
        $addr = [System.Net.Dns]::GetHostAddresses($name) | Select-Object -First 1
        if ($addr) { return $addr.IPAddressToString }
    } catch { }
    return $null
}

function Test-PrivateAddress([string]$ip) {
    if ([string]::IsNullOrEmpty($ip)) { return $true }   # 解析不出来也按异常处理
    if ($ip -match '^(127\.|10\.|192\.168\.|169\.254\.|0\.)') { return $true }
    if ($ip -match '^172\.(1[6-9]|2[0-9]|3[0-1])\.') { return $true }
    if ($ip -match '^(::1|f[cd]|fe80)') { return $true }
    return $false
}

function Test-Port([string]$hostName, [int]$port, [int]$timeoutMs = 3000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($hostName, $port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne($timeoutMs)) {
            $client.EndConnect($iar)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if ([string]::IsNullOrEmpty($HomeDir)) { $HomeDir = $HOME }
if ([string]::IsNullOrEmpty($HomeDir)) { $HomeDir = $env:USERPROFILE }

$ghIp = Resolve-First 'github.com'
$altIp = Resolve-First 'ssh.github.com'

$need = $false
$reason = ''
if ([string]::IsNullOrEmpty($ghIp)) {
    $need = $true; $reason = 'github.com 无法解析'
} elseif (Test-PrivateAddress $ghIp) {
    $need = $true; $reason = "github.com 被解析到 $ghIp(本机或私有地址), DNS 遭劫持"
} elseif (-not (Test-Port 'github.com' 22)) {
    $need = $true; $reason = 'github.com:22 不通(网络封锁 22 端口或 SSH 被拦)'
}

Write-Host '==> 检测 GitHub SSH 连通性'
Write-Host "    github.com      -> $(if ($ghIp) { $ghIp } else { '解析失败' })"
Write-Host "    ssh.github.com  -> $(if ($altIp) { $altIp } else { '解析失败' })"
if ($need) {
    Write-Host "    判定: 需要走 443 入口 ($reason)"
} else {
    Write-Host '    判定: 直连 22 端口正常, 无需改动'
}

if ($Mode -eq 'never') {
    Write-Host '    -Mode never: 只检测, 未写入。'
    exit 0
}
if ($Mode -eq 'auto' -and -not $need) {
    Write-Host '    未检测到问题, 跳过(想强制写入用 -Mode always)。'
    exit 0
}

$sshDir = Join-Path $HomeDir '.ssh'
$cfg = Join-Path $sshDir 'config'
$begin = '# >>> hublane: GitHub SSH over 443 >>>'
$end = '# <<< hublane: GitHub SSH over 443 <<<'

if ((Test-Path $cfg) -and (Select-String -Path $cfg -SimpleMatch -Quiet -Pattern $begin)) {
    Write-Host "==> $cfg 已含本段配置, 跳过(幂等)"
    exit 0
}

Write-Host "==> 写入 $cfg"
$block = @"

$begin
# 由 hublane 写入。原因: github.com 的 DNS 被劫持到本机/私有地址，或网络封锁 22
# 端口，导致 ssh 连不到真正的 GitHub。改用 GitHub 官方的 ssh.github.com:443 入口。
# 删掉两个标记之间的内容即可恢复默认行为。
Host github.com
  HostName ssh.github.com
  Port 443
  User git
$end
"@

if ($DryRun) {
    Write-Host '    (DryRun) 将追加:'
    Write-Host '      Host github.com -> ssh.github.com:443, User git'
    exit 0
}

if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }

# 用无 BOM 的 UTF-8 追加, 避免 BOM 干扰 ssh 的配置解析
$enc = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::AppendAllText($cfg, $block, $enc)
Write-Host '    已追加(原有内容未改动)'

if (Get-Command ssh -ErrorAction SilentlyContinue) {
    Write-Host '    验证(需已把 SSH 公钥加到 GitHub):'
    try {
        $out = & ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1
        $out | Select-Object -First 2 | ForEach-Object { Write-Host "      $_" }
    } catch {
        Write-Host "      验证未通过: $_"
    }
} else {
    Write-Host '    未找到 ssh 命令, 请自行验证: ssh -T git@github.com'
}
