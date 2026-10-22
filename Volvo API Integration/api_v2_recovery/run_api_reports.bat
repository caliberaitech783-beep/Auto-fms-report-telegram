@echo off
cd /d %~dp0
python api_report_runner.py >> logs\scheduled_run.log 2>&1

