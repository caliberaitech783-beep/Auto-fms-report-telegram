@echo off
cd /d D:\VolvoAutomation_APIv2
python volvo_api_v2.py --site "Jayant East" --start 2026-07-23T12:00:00 --stop 2026-07-23T15:00:00 --no-scores --csv output\i_shift_validation_1200_1500.csv > logs\i_shift_validation.log 2>&1
