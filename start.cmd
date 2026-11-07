@echo off
cd /d "%~dp0"
python start_windows.py
if errorlevel 1 pause
