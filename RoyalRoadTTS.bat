@echo off
:: Launch Royal Road TTS in the system tray (no console window)
cd /d "%~dp0"
start "" /B .venv\Scripts\pythonw.exe tray.py
