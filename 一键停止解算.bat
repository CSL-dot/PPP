@echo off
chcp 65001 >nul
title 停止指定解算服务
color 0C
cls

echo ====================================================
echo     仅停止：数据流 + PPP解算 + 气象存储 + BRDM
echo                不影响其他Python程序
echo ====================================================
echo.

:: 要停止的脚本路径（和启动器完全一致）
set "S0=brdm_downloader.py"
set "S1=rt_stream32_lowcpu2.py"
set "S2=controller_master.py"
set "S3=saveairport.py"
set "S4=sp3_downloader.py"


echo 正在停止：%S0%
wmic process where "commandline like '%%%S0%%%'" delete >nul 2>&1

echo 正在停止：%S1%
wmic process where "commandline like '%%%S1%%%'" delete >nul 2>&1

echo 正在停止：%S2%
wmic process where "commandline like '%%%S2%%%'" delete >nul 2>&1

echo 正在停止：%S3%
wmic process where "commandline like '%%%S3%%%'" delete >nul 2>&1


echo 正在停止：%S4%
wmic process where "commandline like '%%%S4%%%'" delete >nul 2>&1
echo.
echo ====================================================
echo              ✅ 服务已安全停止
echo ====================================================
pause >nul