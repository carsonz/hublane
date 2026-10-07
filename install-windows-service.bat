@echo off
setlocal enabledelayedexpansion
title hublane 服务模式安装 (需管理员)

REM ============================================================
REM  把 hublane 注册成真正的 Windows 服务:
REM    - 开机即运行, 与登录状态无关(注销后依然工作)
REM    - 崩溃后由 SCM 自动重启(sc failure)
REM    - 支持 sc stop / net stop 优雅退出
REM  前置条件: 已运行过 install-windows.bat (部署 + 证书 + CA)
REM ============================================================

set "DEST=%LOCALAPPDATA%\hublane"
set "SVC=hublane"
set "PORT=8899"

REM ---------- 0. 管理员检查 ----------
net session >nul 2>&1
if errorlevel 1 (
  echo [错误] 需要管理员权限: 请右键"以管理员身份运行"本脚本。
  echo        原因: 注册系统服务(sc create)必须提升权限。
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
  echo [错误] 未找到 %DEST%\server.crt (证书缺失)
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

REM ---------- 3. 移除登录自启(避免与服务重复) ----------
echo [3/6] 移除计划任务 / Run 项 (改为服务自启)
schtasks /end    /tn %SVC% >nul 2>&1
schtasks /delete /tn %SVC% /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v %SVC% /f >nul 2>&1
reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v %SVC% /f >nul 2>&1

REM ---------- 4. 创建服务 ----------
echo [4/6] 创建服务 %SVC%
sc query %SVC% >nul 2>&1
if not errorlevel 1 (
  echo   已存在同名服务, 先停止并删除
  sc stop %SVC% >nul 2>&1
  timeout /t 2 >nul
  sc delete %SVC% >nul 2>&1
  timeout /t 1 >nul
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
echo [5/6] 配置崩溃自动重启 (sc failure)
sc failure %SVC% reset= 86400 actions= restart/3000/restart/3000/restart/3000 >nul 2>&1

REM ---------- 6. 启动 ----------
echo [6/6] 启动服务
sc start %SVC% >nul
if errorlevel 1 (
  echo   [警告] 启动失败, 请查看事件查看器或 %DEST%\hublane.log
) else (
  timeout /t 3 >nul
  sc query %SVC% | findstr /i "RUNNING" >nul
  if errorlevel 1 (
    echo   [警告] 服务未处于 RUNNING, 请检查 %DEST%\hublane.log
  ) else (
    echo   RUNNING
  )
)

echo.
echo ============================================
echo   完成(服务模式: 开机自启, 注销后仍运行)
echo     状态  : sc query %SVC%     ^|  net stop %SVC%
echo     代理  : 127.0.0.1:%PORT%  (HTTP + SOCKS5)
echo     面板  : http://127.0.0.1:28898/
echo     日志  : %DEST%\hublane.log
echo     卸载  : 以管理员身份运行 uninstall-windows-service.bat
echo ============================================
endlocal
