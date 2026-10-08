@echo off
chcp 65001 >nul
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
sc stop %SVC% >nul 2>&1
timeout /t 2 >nul
sc delete %SVC% >nul 2>&1
sc query %SVC% >nul 2>&1
REM 用标签而不是单行 if/else: cmd 解析括号块时, 同一行既有中文又有
REM ASCII 括号会被 DBCS 解码错位吞掉右括号, 报"此时不应有 右括号"。
sc delete %SVC% >nul 2>&1
if errorlevel 1 goto svc_still_there
echo    服务已删除
goto svc_check_done
:svc_still_there
echo    [警告] 服务仍在, 可稍后重试 sc delete %SVC%
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
