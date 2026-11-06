@echo off
chcp 65001 >nul
node "%~dp0stack.mjs" %*
if "%~1"=="" pause
