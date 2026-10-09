@echo off
chcp 936 >nul
setlocal
title hublane 服务模式卸载 (需管理员)

set "DEST=%LOCALAPPDATA%\hublane"
set "SVC=hublane"

echo ============================================
echo   hublane 服务模式卸载
echo ============================================
echo.

net session >nul 2>&1
if errorlevel 1 (
  echo [错误] 需要管理员权限: 请右键"以管理员身份运行"本脚本。
  exit /b 1
)

echo [1/4] 停止并删除服务
REM 顺序很关键: 先清空服务的"崩溃自动重启"动作, 再停/杀进程。
REM 否则杀掉服务进程会被 SCM 判定为崩溃, 按 sc failure 的 restart 动作在几秒后
REM 自动拉起 -> 服务永远停在 RUNNING, sc delete 只能"标记删除", 条目不消失。
sc query %SVC% >nul 2>&1
if not errorlevel 1 sc failure %SVC% reset= 0 actions= "" >nul 2>&1
sc stop %SVC% >nul 2>&1
ping -n 3 127.0.0.1 >nul
REM 先杀掉服务拉起的进程: 否则 sc delete 只"标记删除", 进程不退则服务条目不消失
for /f "tokens=3" %%p in ('sc queryex %SVC% 2^>nul ^| findstr /R "PID *:"') do taskkill /F /PID %%p >nul 2>&1
ping -n 2 127.0.0.1 >nul
sc delete %SVC% >nul 2>&1
REM 轮询直到条目彻底消失(sc delete 是异步的), 否则会误报"服务仍在"
set "SVC_GONE="
for /l %%i in (1,1,30) do (
  if not defined SVC_GONE (
    sc query %SVC% >nul 2>&1
    if errorlevel 1 (set "SVC_GONE=1") else (ping -n 2 127.0.0.1 >nul)
  )
)
REM 用 sc query 的存在性(而非 sc delete 的报错)判定: 不存在=已删, 存在=仍在
if defined SVC_GONE goto svc_gone
echo    [警告] 服务仍在, 可稍后重试 sc delete %SVC%
goto svc_check_done
:svc_gone
echo    服务已删除(或本就不存在)
:svc_check_done


echo [2/4] 结束残留进程
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8899" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":28898" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

echo [3/4] 关闭系统代理
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul

echo [4/4] 保留配置与证书于 %DEST%
echo.
echo 完成。若还想保留"登录自启"计划任务模式, 可重新运行 install-windows.bat
echo 彻底清理请运行 uninstall-windows.bat
endlocal
