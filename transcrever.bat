@echo off
chcp 65001 >nul
cd /d "%~dp0"
python transcrever_audios.py %*
echo.
pause