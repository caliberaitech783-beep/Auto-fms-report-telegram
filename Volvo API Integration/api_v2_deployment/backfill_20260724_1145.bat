@echo off
cd /d D:\VolvoAutomation_APIv2
python api_report_runner.py --as-of 2026-07-24T11:45:00 >> logs\backfill_20260724_1145.log 2>&1
