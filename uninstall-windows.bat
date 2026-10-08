@echo off
chcp 65001 >nul
setlocal
title hublane 卸载

set "DEST=%LOCALAPPDATA%\hublane"

echo ============================================
echo   hublane 卸载
echo ============================================
echo.

echo [1/5] 关闭系统代理
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul

echo [2/5] 移除计划任务与 Run 项
schtasks /end   /tn hublane >nul 2>&1
schtasks /delete /tn hublane /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v hublane /f >nul 2>&1

echo [3/5] 移除本地 CA
certutil -delstore -user Root "hublane Local Relay CA" >nul 2>&1
certutil -delstore      Root "hublane Local Relay CA" >nul 2>&1

echo [4/5] 结束残留进程
REM run-loop.bat 是 goto loop 死循环: 只杀 python.exe 没用, 监管它的 cmd.exe
REM 会在 3 秒后重新拉起。先按命令行杀掉整个进程树, 再按端口兜底。
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run-loop*' -or $_.CommandLine -like '*hublane.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8899" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":28898" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

echo [5/5] 清理自启脚本
del /Q "%DEST%\run-loop.bat" >nul 2>&1
del /Q "%DEST%\launch.vbs"   >nul 2>&1

echo.
echo 完成。配置与证书保留在 %DEST%，如需彻底删除请手动删除该目录。
echo 重新部署: install-windows.bat
endlocal
