@echo off
cd /d "%~dp0"
python "%~dp0apk_release_builder.py"
if errorlevel 1 pause
