@echo off
REM Launch DocuPilot AI (Windows). Run setup first: see README.md
cd /d "%~dp0"
".venv\Scripts\python.exe" -m streamlit run app.py
