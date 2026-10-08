@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title hublane for Windows

set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\hublane"
set "PORT=8899"
REM 让内嵌 Python 用 UTF-8 读写, 否则中文输出在 cmd 里是乱码
set "PYTHONUTF8=1"

echo ============================================
echo   hublane for Windows - 替代 Watt Toolkit
echo ============================================
echo.

REM ---------- 1. 查找 Python ----------
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (
  echo [错误] 未找到 Python。
  echo        请先安装:  winget install Python 3.12
  exit /b 1
)
echo [1/6] Python: %PYEXE%
REM 解析成绝对路径: 计划任务 / Run 项的运行环境不一定有 py 启动器,
REM 只写 "py" 会让 run-loop.bat 静默失败并陷入 3 秒死循环。
for /f "delims=" %%p in ('%PYEXE% -c "import sys;print(sys.executable)"') do set "PYEXE=%%p"
"%PYEXE%" -V

REM ---------- 2. 部署 ----------
echo [2/6] 部署到 %DEST%
if not exist "%DEST%" mkdir "%DEST%"
copy /Y "%SRC%hublane.py"  "%DEST%\" >nul
copy /Y "%SRC%config.json" "%DEST%\" >nul

REM ---------- 2b. 本地生成 CA + 叶子证书 (不向仓库提交任何私钥) ----------
call :gen_certs "%DEST%"
if errorlevel 1 exit /b 1

REM ---------- 3. P2 配置校验 ----------
echo [3/6] 配置校验
"%PYEXE%" "%DEST%\hublane.py" --config "%DEST%\config.json" --check
if errorlevel 1 (
  echo   [错误] 配置校验未通过, 请修正 %DEST%\config.json
  exit /b 1
)

REM ---------- 4. 安装 CA ----------
REM 这里刻意用标签跳转而不是 if ... ( ... ) else ( ... ):
REM cmd.exe 解析括号块时, 同一行里既有中文又有 ASCII 括号会被 DBCS 解码
REM 错位吞掉右括号, 报"此时不应有 右括号"之类的解析错, 而且恰好在报错分支里。
echo [4/6] 安装本地 CA 到当前用户受信任根证书颁发机构
certutil -addstore -user -f Root "%DEST%\ca.crt" >nul 2>&1
if errorlevel 1 goto ca_machine
echo   CA 已安装 - 当前用户
goto ca_done
:ca_machine
certutil -addstore -f Root "%DEST%\ca.crt" >nul 2>&1
if errorlevel 1 goto ca_fail
echo   CA 已安装 - 本地计算机
goto ca_done
:ca_fail
echo   [警告] CA 安装失败, 请以管理员身份重新运行本脚本
:ca_done

REM ---------- 5. P1 自愈式启动 (计划任务 + 崩溃重启循环) ----------
echo [5/6] 创建自愈式启动 - 计划任务, 崩溃后 3 秒自动重启
> "%DEST%\run-loop.bat" (
  echo @echo off
  echo :loop
  echo "%PYEXE%" "%DEST%\hublane.py" --config "%DEST%\config.json" ^>^> "%DEST%\hublane.log" 2^>^&1
  echo timeout /t 3 ^>nul
  echo goto loop
)
> "%DEST%\launch.vbs" (
  echo Set sh = CreateObject^("Wscript.Shell"^)
  echo sh.Run "cmd /c ""%DEST%\run-loop.bat""", 0, False
)
schtasks /create /tn hublane /sc ONLOGON /tr "wscript.exe \"%DEST%\launch.vbs\"" /rl LIMITED /f >nul 2>&1
if errorlevel 1 (
  echo   [警告] 计划任务创建失败, 改为注册表 Run 项
  reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v hublane ^
      /t REG_SZ /d "wscript.exe \"%DEST%\launch.vbs\"" /f >nul
)
schtasks /run /tn hublane >nul 2>&1
if errorlevel 1 start "" wscript.exe "%DEST%\launch.vbs"
timeout /t 3 >nul

REM ---------- 6. 系统代理 ----------
echo [6/6] 设置系统代理 127.0.0.1:%PORT%
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable   /t REG_DWORD /d 1 /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer   /t REG_SZ /d "127.0.0.1:%PORT%" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyOverride /t REG_SZ /d "localhost;127.*;*.local" /f >nul

echo.
echo ============================================
echo   完成 - 计划任务模式: 登录后自启
echo     代理    : 127.0.0.1:%PORT%  HTTP + SOCKS5
echo     面板    : http://127.0.0.1:28898/
echo     指标/PAC: http://127.0.0.1:28898/status  ^|  /pac
echo     日志    : %DEST%\hublane.log
echo     配置    : %DEST%\config.json
echo     注销后也运行: 以管理员运行 install-windows-service.bat 注册为系统服务
echo     卸载    : uninstall-windows.bat
echo ============================================
echo.

REM ---------- 3.2: Firefox 用自己的信任库, 需单独导入 ----------
set "FFDIR=%APPDATA%\Mozilla\Firefox"
if exist "%FFDIR%\profiles.ini" (
  echo   [提示] 检测到 Firefox: 它**不使用**系统证书库, 需要单独导入 CA
  echo          设置 ^> 隐私与安全 ^> 查看证书 ^> 证书机构 ^> 导入
  echo          文件: %DEST%\ca.crt  勾选"信任由此机构标识的网站"
) else (
  if exist "%ProgramFiles%\Mozilla Firefox\firefox.exe" (
    echo   [提示] 已安装 Firefox: 需在 Firefox 内单独导入 %DEST%\ca.crt
  )
)
endlocal
goto :eof

REM ---------- 本地生成 CA + 叶子证书 (不向仓库提交任何私钥) ----------
:gen_certs
set "GD=%~1"
if exist "%GD%\ca.crt" if exist "%GD%\ca.key" if exist "%GD%\server.crt" if exist "%GD%\server.key" exit /b 0

REM 查找 openssl (Git for Windows 自带)
set "OPENSSL="
for %%P in (
  "%LOCALAPPDATA%\Programs\Git\usr\bin\openssl.exe"
  "C:\Program Files\Git\usr\bin\openssl.exe"
  "C:\Program Files (x86)\Git\usr\bin\openssl.exe"
) do ( if exist %%~P ( set "OPENSSL=%%~P" & goto :openssl_found ) )
where openssl >nul 2>nul && ( set "OPENSSL=openssl" & goto :openssl_found )
echo   [错误] 未找到 openssl, 无法生成本地 CA 证书
echo         请安装 Git for Windows 或 OpenSSL, 然后重新运行本脚本
exit /b 1
:openssl_found
echo   生成本地 CA 与叶子证书, 使用 openssl...
"%OPENSSL%" req -x509 -newkey rsa:2048 -nodes -keyout "%GD%\ca.key" -out "%GD%\ca.crt" -days 3650 -subj "/O=hublane/OU=Local Relay/CN=hublane Local Relay CA" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,digitalSignature,keyCertSign,cRLSign" >nul 2>&1
set "SAN=DNS:ajax.googleapis.com,DNS:api.github.com,DNS:assets.hcaptcha.com,DNS:auth.docker.io,DNS:avatars.githubusercontent.com,DNS:bitbucket.org,DNS:camo.githubusercontent.com,DNS:cdn-lfs.huggingface.co,DNS:cdn.arkoselabs.com,DNS:cdn.jsdelivr.net,DNS:cdnjs.cloudflare.com,DNS:client-api.arkoselabs.com,DNS:cloud.githubusercontent.com,DNS:codeberg.org,DNS:codeload.github.com,DNS:conda.anaconda.org,DNS:crates.io,DNS:dl.dropboxusercontent.com,DNS:docs.rs,DNS:downloads.sourceforge.net,DNS:dropbox.com,DNS:epic-games-api.arkoselabs.com,DNS:esm.sh,DNS:files.pythonhosted.org,DNS:fly.dev,DNS:fonts.googleapis.com,DNS:fonts.gstatic.com,DNS:gcr.io,DNS:ghcr.io,DNS:gist.github.com,DNS:github.com,DNS:github.dev,DNS:github.githubassets.com,DNS:githubusercontent.com,DNS:gitlab.com,DNS:go.dev,DNS:golang.google.cn,DNS:golang.org,DNS:gravatar.com,DNS:hcaptcha.com,DNS:hf.co,DNS:huggingface.co,DNS:imgs.hcaptcha.com,DNS:imgs3.hcaptcha.com,DNS:index.crates.io,DNS:js.hcaptcha.com,DNS:k8s.gcr.io,DNS:mega.co.nz,DNS:mega.io,DNS:mega.nz,DNS:netlify.app,DNS:netlify.com,DNS:newassets.hcaptcha.com,DNS:nodejs.org,DNS:objects.githubusercontent.com,DNS:onedrive.live,DNS:onedrive.live.com,DNS:pages.dev,DNS:private-user-images.githubusercontent.com,DNS:prod-ireland.arkoselabs.com,DNS:production.cloudflare.docker.com,DNS:proxy.golang.org,DNS:pypi.org,DNS:quay.io,DNS:railway.app,DNS:raw.githubusercontent.com,DNS:registry-1.docker.io,DNS:registry.k8s.io,DNS:registry.npmjs.org,DNS:releases.hashicorp.com,DNS:repo.anaconda.com,DNS:secure.gravatar.com,DNS:sourceforge.net,DNS:static.crates.io,DNS:static.rust-lang.org,DNS:sum.golang.org,DNS:themes.googleusercontent.com,DNS:unpkg.com,DNS:user-images.githubusercontent.com,DNS:vercel.app,DNS:workers.dev,DNS:www.dropbox.com,DNS:www.github.com,DNS:www.gravatar.com,DNS:www.hcaptcha.com,DNS:www.mega.nz,DNS:www.npmjs.com,DNS:opencode.ai,DNS:www.baidu.com,DNS:localhost"
>"%GD%\hublane.ext" echo subjectAltName=%SAN%
>>"%GD%\hublane.ext" echo basicConstraints=CA:FALSE
>>"%GD%\hublane.ext" echo extendedKeyUsage=serverAuth
"%OPENSSL%" req -newkey rsa:2048 -nodes -keyout "%GD%\server.key" -out "%GD%\server.csr" -subj "/O=hublane/OU=Local Relay/CN=hublane Relay Leaf" >nul 2>&1
"%OPENSSL%" x509 -req -in "%GD%\server.csr" -CA "%GD%\ca.crt" -CAkey "%GD%\ca.key" -CAcreateserial -out "%GD%\server.crt" -days 3650 -extfile "%GD%\hublane.ext" >nul 2>&1
del /q "%GD%\server.csr" "%GD%\hublane.ext" "%GD%\ca.srl" 2>nul
exit /b 0
