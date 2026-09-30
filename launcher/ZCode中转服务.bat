@echo off
rem 本 bat 为 GBK(936) 编码 + CRLF 换行。双击时 cmd 需为 936，否则中文乱码。
rem 从 PowerShell 7、UTF-8 控制台启动时也需切换，否则乱码。
rem 用 <NUL 防 chcp 抢走 stdin（菜单的 set /p 会读到乱码或直接挂死）
set "TMPCP=%TEMP%\cp_zcode.tmp"
chcp <NUL > "%TMPCP%"
set "CPLINE="
set /p CPLINE=<"%TMPCP%"
set "OLDCP="
if defined CPLINE set "OLDCP=%CPLINE:*: =%"
chcp 936 <NUL >nul
title ZCode 中转服务
set "ZROOT=C:\Users\yegetables\work\zcode2api"
set "ZPORT=3000"
if exist "%ZROOT%\port.txt" set /p ZPORT=<"%ZROOT%\port.txt"
set "TMPF=%TEMP%\netstat_zcode.tmp"

rem ================== 主菜单 ==================
:main
call :refresh
call :checkport %ZPORT%
echo.
echo ==================================================
echo    ZCode 中转服务  (Anthropic Messages 网关)
echo ==================================================
echo    Start Plan 网关 : 端口 %ZPORT%   [%STATE%]
echo    GLM-5.3 / GLM-5.3-Flash
echo    端点            : http://127.0.0.1:%ZPORT%/v1/messages
echo    后台管理        : http://127.0.0.1:%ZPORT%/admin/login
echo ==================================================
echo    [1] 启动服务（后台运行，关掉此窗口不停止）
echo    [2] 停止服务
echo    [3] 测试连通（发一条真实请求）
echo    [4] 查看账号状态
echo    [5] 查看进程
echo    [6] 修改端口
echo    [0] 退出
echo.
set "choice="
set /p choice=请输入数字后回车:
if "%choice%"=="1" goto start
if "%choice%"=="2" goto stop
if "%choice%"=="3" goto test
if "%choice%"=="4" goto accounts
if "%choice%"=="5" call :show_params %ZPORT% "ZCode"
if "%choice%"=="6" goto setport
if "%choice%"=="0" goto quit
goto main

rem ================== 启动 ==================
:start
call :checkport %ZPORT%
if not "%STATE%"=="已停止" goto busy
if not exist "%ZROOT%\main.py" (
    echo.
    echo 找不到项目目录: %ZROOT%
    echo 请检查本文件顶部的 ZROOT 设置。
    pause
    goto main
)
echo.
echo 正在启动... 首次启动约需 3 秒
rem 用 start 的 /D 指定工作目录，避免 cmd /c "cd /d "路径"" 的嵌套引号解析错误
start "ZCode 中转 :%ZPORT%" /D "%ZROOT%" cmd /c "python main.py serve --port %ZPORT%"
timeout /t 4 >nul 2>nul
call :refresh
call :checkport %ZPORT%
echo 当前状态: %STATE%
if "%STATE%"=="已停止" (
    echo.
    echo 启动失败。上面的窗口里应该有报错信息。
    pause
)
goto main

:busy
echo.
echo 端口 %ZPORT% 已被占用（%STATE%），无需重复启动。
echo 要重启请先选 [2] 停止服务。
timeout /t 3 >nul 2>nul
goto main

rem ================== 停止 ==================
:stop
call :refresh
call :checkport %ZPORT%
if "%STATE%"=="已停止" (
    echo.
    echo 端口 %ZPORT% 没有监听，无需停止。
    timeout /t 2 >nul 2>nul
    goto main
)
echo.
echo 正在停止服务（端口 %ZPORT%，%STATE%）...
for /f "tokens=5" %%p in ('findstr /c:":%ZPORT% " "%TMPF%" ^| findstr /c:"LISTENING"') do (
    echo   停止 PID %%p ...
    taskkill /PID %%p /T /F >nul 2>nul
)
timeout /t 2 >nul 2>nul
call :refresh
call :checkport %ZPORT%
echo 停止后状态: %STATE%
timeout /t 2 >nul 2>nul
goto main

rem ================== 连通测试 ==================
:test
call :checkport %ZPORT%
if "%STATE%"=="已停止" (
    echo.
    echo 服务没在跑，请先选 [1] 启动服务。
    timeout /t 3 >nul 2>nul
    goto main
)
echo.
echo 正在发一条真实请求（约需 5-15 秒）...
echo.
curl.exe -s -X POST "http://127.0.0.1:%ZPORT%/v1/messages" -H "content-type: application/json" -d "{\"model\":\"GLM-5.3-Flash\",\"max_tokens\":1024,\"messages\":[{\"role\":\"user\",\"content\":\"只回复两个字：成功\"}]}" --max-time 90
echo.
echo.
echo 若上面是 JSON 且含 content 字段，说明调用正常。
echo 若返回 503 no_available_account：
echo    - 多半是上游 429 限流，账号进了冷却（默认 300 秒）。
echo    - 请等一会儿再试，或到后台把账号状态改回 active。
echo    - 上游限流按频率判定，连续请求间隔建议 30 秒以上。
echo.
pause
goto main

rem ================== 账号状态 ==================
:accounts
echo.
cd /d "%ZROOT%"
python main.py accounts
python main.py status
echo.
echo 说明：参与轮询的应是 mode=start-plan 的账号；exhausted / invalid 的号不会被选中。
echo.
pause
goto main

rem ================== 改端口 ==================
:setport
echo.
set "newport="
set /p newport=请输入新端口（1024-65535，直接回车取消）:
if "%newport%"=="" goto main
call :validate_port %newport%
if not "%OK%"=="1" goto setport
>"%ZROOT%\port.txt" echo %newport%
set "ZPORT=%newport%"
echo.
echo 已保存：服务端口 = %ZPORT%（下次启动生效）
goto main

rem ================== 公共子程序 ==================
rem 刷新一次监听快照，供所有 checkport 调用，避免每个端口都重跑一遍 netstat
:refresh
netstat -ano > "%TMPF%" 2>nul
exit /b

rem 判断 %1 端口是否在监听，结果写进变量 STATE
rem ponytail: 只关心端口是否在监听，不解析进程名；要确认是什么进程时用菜单里的 show_params
:checkport
set "STATE=已停止"
for /f "tokens=5" %%p in ('findstr /c:":%~1 " "%TMPF%" ^| findstr /c:"LISTENING"') do set "STATE=运行中 PID=%%p"
exit /b

rem 显示 %1 端口上的进程命令行；%2 = 显示名称
:show_params
call :refresh
call :checkport %~1
echo.
if "%STATE%"=="已停止" (
    echo   [%2] 未发现端口 %~1 监听。
    pause
    exit /b
)
set "ZPID="
for /f "tokens=5" %%p in ('findstr /c:":%~1 " "%TMPF%" ^| findstr /c:"LISTENING"') do set "ZPID=%%p"
echo   [%2]  端口 %~1  PID %ZPID%
echo   进程命令行:
pwsh -NoProfile -Command "$q = Get-CimInstance Win32_Process -Filter 'ProcessId=%ZPID%'; if ($q) { $q.CommandLine } else { 'n/a' }"
echo.
pause
exit /b

rem 校验 %1 是否为 1024-65535 的合法端口，OK=1 表示合法
:validate_port
set "OK=0"
echo %~1|findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
    echo 请输入有效数字端口号。
    exit /b
)
if %~1 LSS 1024 (
    echo 端口号需大于等于 1024。
    exit /b
)
if %~1 GTR 65535 (
    echo 端口号需小于等于 65535。
    exit /b
)
set "OK=1"
exit /b

rem 退出前恢复原编码，避免影响从本 bat 启动的 PowerShell 7 控制台
:quit
if defined OLDCP chcp %OLDCP% <NUL >nul
exit /b
