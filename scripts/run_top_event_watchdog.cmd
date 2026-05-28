@echo off
setlocal
cd /d C:\python_scripts\top_1
if not exist .agent\control mkdir .agent\control
C:\Python312\python.exe -u scripts\top_event_watchdog.py --loop --sleep-sec 30 --consilium-stable-sec 90 --session-id 019e3182-5823-7201-b156-097511a3a30a >> .agent\control\top_event_watchdog.out.log 2>> .agent\control\top_event_watchdog.err.log
