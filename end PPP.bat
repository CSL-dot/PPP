@echo off
chcp 65001 >nul

mode con: cols=60 lines=19

title 【一键解算面板】
color 0A
cls

echo ====================================================
echo                服务总控启动器
echo ====================================================
echo.

:: ===================== 配置 =====================
set "PY_PATH=C:\Users\Administrator\AppData\Local\Programs\Python\Python39\python.exe"
set "PYW_PATH=C:\Users\Administrator\AppData\Local\Programs\Python\Python39\pythonw.exe"

set "SCRIPT0=E:\PyProj\WUHAN\brdm_downloader.py"
set "SCRIPT1=E:\PyProj\WUHAN\rt_stream32_lowcpu2.py"
set "SCRIPT2=E:\PyProj\WUHAN\controller_master.py"
set "SCRIPT3=E:\PyProj\Airport\PT\saveairport.py"
set "SCRIPT4=E:\PyProj\LSTM-新建模-无速率增长率\test.py"

:: ===================== 检查Python =====================
if not exist "%PY_PATH%" (
    color 0C
    echo 【错误】未找到 Python！
    echo 路径：%PY_PATH%
    pause >nul
    exit /b
)

echo ✅ Python 环境正常
echo.

:: ===================== 启动脚本 =====================
echo 正在启动服务...
echo.

:: 启动 BRDM接入（防重复启动）
::tasklist /v | find /i "brdm_downloader.py" >nul
::if %errorlevel%==0 (
::    echo ⚠ BRDM接入已运行，跳过启动
::) else (
::    if exist "%SCRIPT0%" (
::        start "BRDM接入" "%PY_PATH%" "%SCRIPT0%"
::        echo ✅ 已启动：BRDM接入
::        timeout /t 2 /nobreak >nul
::    ) else (
::        echo ❌ 不存在：%SCRIPT0%
::    )
::)




:: 启动 数据流接入
if exist "%SCRIPT1%" (
    start "数据流接入" cmd /c "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & "%PY_PATH%" "%SCRIPT1%" 2>&1"
    echo ✅ 已启动：数据流接入
    timeout /t 2 /nobreak >nul
) else (
    echo ❌ 不存在：%SCRIPT1%
)
echo.



:: 启动 实时PPP解算
if exist "%SCRIPT2%" (
    start "实时PPP解算" cmd /c "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & "%PY_PATH%" "%SCRIPT2%" 2>&1"
    echo ✅ 已启动：实时PPP解算
    timeout /t 2 /nobreak >nul
) else (
    echo ❌ 不存在：%SCRIPT2%
)
echo.

:: 启动 气象数据存储（✅ 已修复：先进入脚本目录再运行）
if exist "%SCRIPT3%" (
    cd /d E:\PyProj\Airport\PT\
	start "" "%PY_PATH%" "%SCRIPT3%"
    echo ✅ 已启动：气象数据存储
) else (
    echo ❌ 不存在：%SCRIPT3%
)
echo.

echo.
echo ====================================================
echo                所有服务启动完成
echo ====================================================
pause >nul
