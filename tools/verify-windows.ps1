<#
.SYNOPSIS
    hublane Windows 管理员级实测: 计划任务模式 + 服务模式全流程验证。

.DESCRIPTION
    必须在管理员会话运行(脚本自身会检查并拒绝)。分两阶段, 每阶段自动清理:
      A 计划任务模式: 安装 -> schtasks 是否真建成 -> run-loop 拉起 ->
        端口 -> /healthz -> 真实 MITM -> 计数 -> 卸载还原
      B 服务模式: sc create -> binPath -> sc qfailure -> sc start ->
        端口/健康 -> 真实 MITM -> 崩溃自愈 -> 优雅停止 -> 卸载还原

.EXAMPLE
    pwsh -NoProfile -Command "Start-Process pwsh -Verb RunAs -ArgumentList '-NoProfile','-File','tools\verify-windows.ps1' -Wait"
#>
[CmdletBinding()]
param([string]$LogPath = "", [switch]$KeepInstalled)

$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $LogPath) { $LogPath = Join-Path $Repo "tools\verify-windows.log" }
$Svc = "hublane"
$script:Fails = 0
$script:Checks = 0

function Say($m) {
    $line = "[{0:HH:mm:ss}] {1}" -f (Get-Date), $m
    Write-Host $line; Add-Content -Path $LogPath -Value $line -Encoding UTF8
}
function Check($name, $cond, $detail = "") {
    $script:Checks++
    if ($cond) { Say "  [PASS] $name  $detail" }
    else { $script:Fails++; Say "  [FAIL] $name  $detail" }
}
function Section($t) { Say ""; Say "=== $t ===" }
function Port-Listening($p) { [bool](netstat -ano | Select-String (":$p\s+\S+\s+LISTENING")) }
function Pid-Listening($p) {
    $l = netstat -ano | Select-String (":$p\s+\S+\s+LISTENING") | Select-Object -First 1
    if (-not $l) { return 0 }
    return [int]((($l.ToString() -split '\s+') | Where-Object { $_ })[-1])
}
function Svc-State { sc.exe query $Svc 2>&1 | Out-String }
function Stop-Anything {
    foreach ($p in @(8899, 28898)) {
        $id = Pid-Listening $p
        if ($id -gt 0) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds 1
}
function Wait-Listener($p, $sec) {
    foreach ($i in 1..$sec) { if (Port-Listening $p) { return $true }; Start-Sleep -Seconds 1 }
    return $false
}
function Wait-Running($sec) {
    foreach ($i in 1..$sec) { if ((Svc-State) -match "RUNNING") { return $true }; Start-Sleep 1 }
    return $false
}
function Smoke-Mitm {
    $ca = Join-Path $env:LOCALAPPDATA "hublane\ca.crt"
    if (-not (Test-Path $ca)) { return "no-ca" }
    $o = & curl.exe -s -o NUL -w "%{http_code}" --proxy http://127.0.0.1:8899 `
        --cacert $ca --ssl-no-revoke --max-time 45 `
        "https://raw.githubusercontent.com/torvalds/linux/master/README" 2>&1
    ($o | Out-String).Trim()
}
function Get-Counters {
    try { (Invoke-RestMethod "http://127.0.0.1:28898/status" -TimeoutSec 8).counters } catch { $null }
}
function Hit-Healthz {
    try { (Invoke-WebRequest "http://127.0.0.1:28898/healthz" -TimeoutSec 8 -UseBasicParsing).Content.Trim() }
    catch { "ERR: $($_.Exception.Message)" }
}
function Install($bat) {
    $o = & cmd.exe /c (Join-Path $Repo $bat) 2>&1
    $t = ($o | Out-String)
    Say "    ---- $bat 原始输出 ----"
    foreach ($l in ($t -split "`r?`n")) { if ($l.Trim()) { Say "    | $l" } }
    @{ code = $LASTEXITCODE; text = $t }
}
function Cleanup {
    Section "清理 Cleanup"
    Stop-Anything
    foreach ($bat in @("uninstall-windows-service.bat", "uninstall-windows.bat")) {
        $p = Join-Path $Repo $bat
        if (Test-Path $p) { Say "  运行 $bat"; & cmd.exe /c $p *>&1 | Out-Null }
    }
    schtasks /delete /tn $Svc /f 2>&1 | Out-Null
    Stop-Anything
    Remove-Item (Join-Path $env:LOCALAPPDATA "hublane") -Recurse -Force -ErrorAction SilentlyContinue
    $run = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $Svc -ErrorAction SilentlyContinue
    Check "Run 键已删除" (-not $run)
    $pe = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -ErrorAction SilentlyContinue).ProxyEnable
    Check "系统代理已关闭" ($pe -eq 0) "ProxyEnable=$pe"
    Check "信任库 CA 已移除" (-not (certutil -store -user Root 2>$null | Select-String "hublane Local Relay CA"))
    Check "8899 已释放" (-not (Port-Listening 8899))
    Check "28898 已释放" (-not (Port-Listening 28898))
    Check "服务已删除" ([bool](Svc-State | Select-String "1060"))
}

Set-Content -Path $LogPath -Value "" -Encoding UTF8
# 关键: 安装脚本内部 chcp 65001 后输出的是 UTF-8 字节, 这里必须同步按 UTF-8
# 解码, 否则日志里会出现假报错(把 UTF-8 当 GBK 读) —— 那是量具的问题不是被测物的问题。
try {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $OutputEncoding = [Text.Encoding]::UTF8
} catch { Say "无法设置控制台编码: $($_.Exception.Message)" }

Say "hublane Windows 管理员级实测"
Say "仓库: $Repo"
$wi = [Security.Principal.WindowsIdentity]::GetCurrent()
$wp = New-Object Security.Principal.WindowsPrincipal($wi)
if (-not $wp.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say "[FATAL] 非管理员会话, 无法实测 sc create / schtasks。"
    exit 2
}
Say "管理员: OK ($($wi.Name))"
Say "Python: $(& py -3 -V 2>&1)"

# ---- 环境体检: 先确认这台机器上"提权"到底给了哪些能力 ----
Section "环境体检 Environment probe"
$v = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
Say "    Edition       = $($v.EditionID) / $($v.ProductName)"
$ls = Get-Service LanmanServer -ErrorAction SilentlyContinue
Say "    LanmanServer  = $(if ($ls) { "$($ls.Status) start=$($ls.StartType)" } else { 'NOT PRESENT (net session 会误报失败!)' })"
$ns = cmd /c "net session" 2>&1
Say "    net session   = exit $LASTEXITCODE  out=$(($ns | Out-String).Trim())"
$fl = cmd /c "fltmc" 2>&1
Say "    fltmc         = exit $LASTEXITCODE"
$scT = cmd /c "sc create hubdiag binPath= `"C:\Windows\System32\cmd.exe /c exit 0`" start= demand" 2>&1
Say "    sc create     = exit $LASTEXITCODE  out=$(($scT | Out-String).Trim())"
cmd /c "sc delete hubdiag" 2>&1 | Out-Null
$st = cmd /c "schtasks /create /tn hubdiag2 /sc ONLOGON /tr `"C:\Windows\System32\cmd.exe /c exit 0`" /rl LIMITED /f" 2>&1
Say "    schtasks      = exit $LASTEXITCODE  out=$(($st | Out-String).Trim())"
cmd /c "schtasks /delete /tn hubdiag2 /f" 2>&1 | Out-Null
Say "    >>> install-*.bat 的管理员判定用的是 'net session', 若上面 net session 非 0"
Say "    >>> 而 token 是管理员, 说明这台机器(多为 Windows 家庭版)上 net session 不可用"

try {
    Section "阶段 A: 计划任务模式 install-windows.bat"
    Stop-Anything
    $a = Install "install-windows.bat"
    Check "install-windows.bat 退出码 0" ($a.code -eq 0) "exit=$($a.code)"
    Check "配置校验通过" ($a.text -match "配置校验通过")
    $dest = Join-Path $env:LOCALAPPDATA "hublane"
    Check "已部署 hublane.py" (Test-Path (Join-Path $dest "hublane.py"))
    Check "已生成 CA" (Test-Path (Join-Path $dest "ca.crt"))
    $loop = Get-Content (Join-Path $dest "run-loop.bat") -Raw
    Check "run-loop.bat 用绝对解释器路径" ($loop -match '"[A-Za-z]:\\[^"]*python(w)?\.exe"')
    $task = schtasks /query /tn $Svc 2>&1
    $taskOk = ($LASTEXITCODE -eq 0) -and -not ($task | Select-String "ERROR")
    Check "schtasks 计划任务已创建" $taskOk
    if ($taskOk) { Say "    $(($task | Select-String 'Task To Run'))" }

    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$dest\run-loop.bat`"" -WindowStyle Hidden | Out-Null
    $ok1 = Wait-Listener 8899 20
    $ok2 = Wait-Listener 28898 10
    Check "run-loop 拉起后 8899 在听" $ok1
    Check "run-loop 拉起后 28898 在听" $ok2
    if ($ok2) { $hz = Hit-Healthz; Check "/healthz 返回 ok" ($hz -eq "ok") "got=$hz" }
    if ($ok1) {
        $code = Smoke-Mitm
        Say "    MITM 探测: HTTP $code"
        Check "真实 MITM 中继成功(200)" ($code -eq "200") "code=$code"
        Start-Sleep -Seconds 2
        $c = Get-Counters
        if ($c) {
            Say "    counters: requests=$($c.requests) ok=$($c.ok) fail=$($c.fail) bytes=$($c.bytes) tunnel=$($c.tunnel_conns)"
            Check "MITM 请求已计数" ($c.requests -gt 0) "requests=$($c.requests)"
        } else { Check "读取 /status 成功" $false }
    }
    Cleanup

    Section "阶段 B: 服务模式 install-windows-service.bat"
    Stop-Anything
    # 服务模式的前置条件是"已运行过 install-windows.bat"(部署+证书+CA),
    # 而阶段 A 结尾的 Cleanup 把部署目录删了, 这里先补一次部署。
    Say "  重新部署(服务模式前置条件)"
    $pre = Install "install-windows.bat"
    Check "前置部署成功" ($pre.code -eq 0) "exit=$($pre.code)"
    Check "前置部署已就位" (Test-Path (Join-Path $env:LOCALAPPDATA "hublane\hublane.py"))
    $b = Install "install-windows-service.bat"
    Check "install-windows-service.bat 退出码 0" ($b.code -eq 0) "exit=$($b.code)"
    $q = Svc-State
    Check "服务已创建" (($q -match "STATE") -and ($q -notmatch "1060")) ($q -replace "`r?`n", " ")
    # 服务模式应移除计划任务/Run 项, 否则会与服务抢同一批端口
    $taskGone = cmd /c "schtasks /query /tn $Svc" 2>&1 | Select-String "ERROR"
    Check "计划任务已被移除" ([bool]$taskGone)
    $runKey = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $Svc -ErrorAction SilentlyContinue
    Check "Run 项已被移除" (-not $runKey)
    $bps = (sc.exe qc $Svc 2>&1 | Select-String "BINARY_PATH_NAME" | Out-String)
    Say "    $($bps.Trim())"
    Check "binPath 含 --service" ($bps -match "--service")
    Check "binPath 含 --config" ($bps -match "--config")
    Check "binPath 用 pythonw.exe" ($bps -match "pythonw\.exe")
    $qf = sc.exe qfailure $Svc 2>&1 | Out-String
    Check "崩溃自动重启已配置" (($qf -match "RESTART") -or ($qf -match "FailureActions")) `
        (($qf -split "`r?`n" | Where-Object { $_ -match "RECOVERY|RESTART" }) -join " | ")

    Say "  sc start $Svc ..."
    sc.exe start $Svc | Out-Null
    Check "服务状态 RUNNING" (Wait-Running 25)
    $s1 = Wait-Listener 8899 15
    $s2 = Wait-Listener 28898 10
    Check "服务模式 8899 在听" $s1
    Check "服务模式 28898 在听" $s2
    if ($s2) { $hz = Hit-Healthz; Check "服务模式 /healthz 返回 ok" ($hz -eq "ok") "got=$hz" }
    if ($s1) {
        $code = Smoke-Mitm
        Say "    MITM 探测: HTTP $code"
        Check "服务模式 MITM 中继成功(200)" ($code -eq "200") "code=$code"
        Start-Sleep -Seconds 2
        $c = Get-Counters
        if ($c) { Say "    counters: requests=$($c.requests) ok=$($c.ok) bytes=$($c.bytes)" }
    }

    Section "崩溃自愈 Crash recovery"
    $old = Pid-Listening 8899
    Check "取到服务进程 PID" ($old -gt 0) "pid=$old"
    if ($old -gt 0) {
        Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
        Say "  已强杀 pid=$old, 等待服务自动拉起 ..."
        Start-Sleep -Seconds 5
        Check "崩溃后服务自动恢复 RUNNING" (Wait-Running 30)
        Check "崩溃后 8899 重新在听" (Wait-Listener 8899 20)
    }

    Section "优雅停止 Graceful stop"
    $t0 = Get-Date
    sc.exe stop $Svc | Out-Null
    $stopped = $false
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 500
        if ((Svc-State) -match "STOPPED") { $stopped = $true; break }
    }
    $tStopped = ((Get-Date) - $t0).TotalSeconds
    Check "sc stop 后状态 STOPPED" $stopped ("耗时 {0:N1}s" -f $tStopped)

    # 端口释放允许几秒级延迟(SCM 停止 + accept 1s 超时 + 套接字回收),
    # 这里测量实际耗时而不是要求"立刻"释放。
    $released = $false
    $tRel = -1.0
    for ($i = 1; $i -le 40; $i++) {
        if (-not (Port-Listening 8899)) { $released = $true; $tRel = ((Get-Date) - $t0).TotalSeconds; break }
        Start-Sleep -Milliseconds 250
    }
    Check "8899 在 10s 内释放" $released ("耗时 {0:N1}s" -f $tRel)
    if (-not $released) {
        # 端口还被占着却没有 pythonw 进程 -> 必须查清到底谁持有, 不能放过
        $row = netstat -ano | Select-String ":8899\s+\S+\s+LISTENING" | Select-Object -First 1
        Say "    !! 8899 仍在监听: $($row.ToString().Trim())"
        if ($row) {
            $owner = [int]((($row.ToString() -split '\s+') | Where-Object { $_ })[-1])
            Say "    !! 持有者 PID=$owner"
            Say "    !! tasklist: $((tasklist /fi `"PID eq $owner`" /fo list 2>&1 | Out-String).Trim())"
            Say "    !! 服务状态: $((Svc-State -replace "`r?`n", ' '))"
        }
    }
    # 进程收尾还要等 concurrent.futures 的 atexit 钩子 join 线程池, 池里如果在跑
    # DoH/验真的网络请求, 解释器退出会被拖到这些请求超时为止。
    # hublane 自己给 SCM 报的 STOP_PENDING wait_hint 是 30000ms, 所以判据就对齐到
    # 30s: 超时才算异常(那才是真有"影子实例"卡住)。实测耗时照样打出来。
    for ($i = 1; $i -le 120; $i++) {
        $orphan = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*run-loop*" -or $_.CommandLine -like "*hublane.py*" }
        if (-not $orphan) { break }
        Start-Sleep -Milliseconds 250
    }
    $tExit = ((Get-Date) - $t0).TotalSeconds
    Check "进程在 30s 内退出(对齐 wait_hint, 无影子实例)" (-not $orphan) ("耗时 {0:N1}s" -f $tExit)
    if ($orphan) {
        Say "    !! 残留: $(($orphan | ForEach-Object { "$($_.Name):$($_.ProcessId)" }) -join ',')"
    }
    Check "28898 已释放" (-not (Port-Listening 28898))
}
finally {
    if ($KeepInstalled) {
        Section "已指定 -KeepInstalled, 跳过清理"
    } else {
        Cleanup
    }
    Say ""
    Say "===== 结果: $($script:Checks - $script:Fails)/$($script:Checks) 通过 ====="
    Say "日志: $LogPath"
    if ($script:Fails -gt 0) { exit 1 }
    exit 0
}
