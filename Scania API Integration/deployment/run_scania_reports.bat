@echo off
cd /d %~dp0\..
if not exist logs mkdir logs
"%~dp0..\.venv\Scripts\python.exe" run_scheduled_reports.py >> logs\scheduled_run.log 2>&1
