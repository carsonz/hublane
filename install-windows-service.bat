@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title hublane 服务模式安装 (需管理员)

REM ============================================================
REM  把 hublane 注册成真正的 Windows 服务:
REM    - 开机即运行, 与登录状态无关(注销后依然工作)
REM    - 崩溃后由 SCM 自动重启(sc failure)
REM    - 支持 sc stop / net stop 优雅退出
REM  前置条件: 已运行过 install-windows.bat (部署 + 证书 + CA)
REM ============================================================

set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\hublane"
set "SVC=hublane"
set "PORT=8899"
REM 让内嵌 Python 用 UTF-8 读写, 否则中文输出在 cmd 里是乱码
set "PYTHONUTF8=1"

REM ---------- 0. 先看是不是只来问用法(不需要管理员) ----------
for %%A in (%*) do (
  if /I "%%~A"=="--help" goto :usage
  if /I "%%~A"=="-h" goto :usage
)

REM ---------- 0b. 管理员检查 ----------
net session >nul 2>&1
if errorlevel 1 (
  echo [错误] 需要管理员权限: 请右键"以管理员身份运行"本脚本。
  echo        原因: 注册系统服务 sc create 必须提升权限。
  exit /b 1
)

echo ============================================
echo   hublane 服务模式安装
echo ============================================
echo.

REM ---------- 1. 检查部署 ----------
if not exist "%DEST%\hublane.py" (
  echo [错误] 未找到 %DEST%\hublane.py
  echo        请先运行 install-windows.bat 完成部署与证书生成。
  exit /b 1
)
if not exist "%DEST%\server.crt" (
  echo [错误] 未找到 %DEST%\server.crt 证书缺失
  echo        请先运行 install-windows.bat。
  exit /b 1
)
echo [1/6] 安装目录: %DEST%

REM ---------- 2. 解析 Python; 服务用无控制台的 pythonw.exe ----------
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (
  echo [错误] 未找到 Python。请先 winget install Python.Python.3.12
  exit /b 1
)
for /f "delims=" %%p in ('%PYEXE% -c "import sys;print(sys.executable)"') do set "PYEXE=%%p"
set "PYW=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PYEXE%"
echo [2/6] Python: %PYW%

REM ---------- 2b. v0.2.0 第 1 条: 透传 --renew-certs / --renew-ca ----------
REM 与 install-windows.bat 同款: 复用 hublane.py 自带的续期实现。
REM --renew-ca 换了 CA, 必须重装信任, 否则服务起来后客户端证书校验全挂。
set "RENEW_FLAG="
for %%A in (%*) do (
  if /I "%%~A"=="--renew-certs" set "RENEW_FLAG=--renew-certs"
  if /I "%%~A"=="--renew-ca" set "RENEW_FLAG=--renew-ca"
)
if not defined RENEW_FLAG goto renew_done
echo [2.5/6] 按 %RENEW_FLAG% 续期证书
"%PYEXE%" "%DEST%\hublane.py" --config "%DEST%\config.json" %RENEW_FLAG%
if errorlevel 1 goto renew_fail
if not "%RENEW_FLAG%"=="--renew-ca" goto renew_done
echo   CA 已更换, 重新安装信任
certutil -addstore -user -f Root "%DEST%\ca.crt" >nul 2>&1
if errorlevel 1 certutil -addstore -f Root "%DEST%\ca.crt" >nul 2>&1
goto renew_done
:renew_fail
echo   [错误] 证书续期失败, 请查看上面的输出
exit /b 1
:renew_done

REM ---------- 3. 移除登录自启(避免与服务重复) ----------
echo [3/6] 移除计划任务 / Run 项, 改为服务自启
schtasks /end    /tn %SVC% >nul 2>&1
schtasks /delete /tn %SVC% /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v %SVC% /f >nul 2>&1
reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v %SVC% /f >nul 2>&1
REM schtasks /end 只结束任务本身, run-loop.bat 拉起的 python.exe 是它的子进程。
REM 关键: run-loop.bat 是个 goto loop 死循环 —— 只杀 python.exe 没用, 监管它的
REM cmd.exe 会在 3 秒后把 python.exe 再拉起来。所以先按命令行杀掉整个 run-loop
REM 进程树, 再按端口兜底。漏掉这一步的后果: Windows 上 SO_REUSEADDR 允许两个
REM 套接字绑同一端口, 服务与"影子实例"并存, 请求被两个进程分走, sc stop 之后
REM 端口看起来一直"没释放"。
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run-loop*' -or $_.CommandLine -like '*hublane.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8899" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":28898" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
REM 等待一律用 ping 而不是 timeout: timeout 在 stdin 不是控制台时
REM (脚本里调脚本/输出被重定向)直接报错退出, 等待落空(Windows 实测)。
ping -n 3 127.0.0.1 >nul

REM ---------- 4. 创建服务 ----------
echo [4/6] 创建服务 %SVC%
sc query %SVC% >nul 2>&1
if not errorlevel 1 (
  echo   已存在同名服务, 先停止并删除
  sc stop %SVC% >nul 2>&1
  ping -n 3 127.0.0.1 >nul
  sc delete %SVC% >nul 2>&1
  ping -n 2 127.0.0.1 >nul
)
sc create %SVC% binPath= "\"%PYW%\" \"%DEST%\hublane.py\" --service --config \"%DEST%\config.json\"" ^
   start= auto DisplayName= "hublane relay proxy" >nul
if errorlevel 1 (
  echo   [错误] sc create 失败。可尝试用 NSSM:
  echo          nssm install %SVC% "%PYW%" "%DEST%\hublane.py --service --config %DEST%\config.json"
  echo          nssm set %SVC% AppDirectory "%DEST%" ^&^& nssm set %SVC% Start SERVICE_AUTO_START
  exit /b 1
)
sc description %SVC% "hublane: 本地中继代理, 让 GitHub 在受限网络下可达(HTTP+SOCKS5 127.0.0.1:%PORT%)" >nul 2>&1

REM ---------- 5. 崩溃自愈 ----------
echo [5/6] 配置崩溃自动重启, sc failure
sc failure %SVC% reset= 86400 actions= restart/3000/restart/3000/restart/3000 >nul 2>&1

REM ---------- 6. 启动 ----------
echo [6/6] 启动服务
sc start %SVC% >nul
if errorlevel 1 (
  echo   [警告] 启动失败, 请查看事件查看器或 %DEST%\hublane.log
) else (
  ping -n 4 127.0.0.1 >nul
  sc query %SVC% | findstr /i "RUNNING" >nul
  if errorlevel 1 (
    echo   [警告] 服务未处于 RUNNING, 请检查 %DEST%\hublane.log
  ) else (
    echo   RUNNING
  )
)

echo.
echo ============================================
echo   完成 - 服务模式: 开机自启, 注销后仍运行
echo     状态  : sc query %SVC%     ^|  net stop %SVC%
echo     代理  : 127.0.0.1:%PORT%  HTTP + SOCKS5
echo     面板  : http://127.0.0.1:28898/
echo     日志  : %DEST%\hublane.log
echo     卸载  : 以管理员身份运行 uninstall-windows-service.bat
echo ============================================

REM ---------- 3.2: Firefox 用自己的信任库, policies.json 自动导入 ----------
REM 与 install-windows.bat 同款(v0.2.0 第 1 条); 只在本机装了 Firefox 时才动作。
if not exist "%SRC%tools\setup-firefox-policy.ps1" goto ff_done
powershell -NoProfile -ExecutionPolicy Bypass -File "%SRC%tools\setup-firefox-policy.ps1" -CaPath "%DEST%\ca.crt"
:ff_done
endlocal
goto :eof

:usage
echo 用法: install-windows-service.bat [选项]   ^(需管理员^)
echo.
echo   --renew-certs    注册服务前续期叶证书, 保留 CA, 系统信任无需重装
echo   --renew-ca       连同 CA 一起续期, 随后重新安装信任
echo   --help           显示本帮助
echo.
echo 前置: 已运行过 install-windows.bat 完成部署与证书生成
exit /b 0
