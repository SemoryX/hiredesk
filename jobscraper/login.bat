@echo off
chcp 936 >nul
title 招聘站登录
cd /d "%~dp0"
set SITE=%~1
if "%SITE%"=="" set SITE=boss
echo 即将打开 Chrome，请在窗口里完成登录（扫码或账号密码）。
echo 登录完成后不要关浏览器，脚本会自动保存登录态。
echo.
".venv\Scripts\python.exe" jobscraper.py login %SITE%
pause