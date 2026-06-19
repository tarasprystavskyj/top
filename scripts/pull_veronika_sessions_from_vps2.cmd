@echo off
setlocal
set ROOT=C:\python_scripts\top_1_zeno3_chart_20260526
set DEST=%ROOT%\obw_platform\_reports\_live\_server_pull_20260614
mkdir "%DEST%" 2>nul
echo Pulling Veronika live report sessions into:
echo %DEST%
echo.
echo This command uses interactive SSH authentication. Do not put passwords in this file.
echo.
scp -o StrictHostKeyChecking=accept-new -r simple_user@vps2.happyuser.info:/var/www/vps2.happyuser.info/top/veronika/obw_platform/_reports/_live/* "%DEST%\"
if errorlevel 1 (
  echo.
  echo Pull failed. Check SSH auth/network and rerun this script.
  exit /b 1
)
echo.
echo Pull complete. Re-run:
echo cd /d %ROOT%
echo python scripts\consolidate_veronika_and_calibrate_slippage.py
