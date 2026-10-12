@echo off
rem Starts Jarvis: opens the HUD window and begins listening for "Hey Jarvis".
rem Close this window (or press Ctrl+C) to stop it and release the microphone.
title Jarvis
cd /d "%~dp0"
".venv\Scripts\python.exe" -u listen.py
pause
