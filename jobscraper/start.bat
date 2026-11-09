@echo off
chcp 936 >nul
title JD 采集看板
cd /d "%~dp0"
echo 正在启动 JD 看板，浏览器稍后自动打开...
echo 用法：保持本窗口不要关；关闭窗口即停止服务。
echo.
start "" http://127.0.0.1:8765/
".venv\Scripts\python.exe" -u web\serve.py
echo.
echo 服务已退出。
pause