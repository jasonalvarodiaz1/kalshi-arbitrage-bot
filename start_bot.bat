@echo off
cd /d "c:\Users\jason\Kalshi Bot\kalshi-arbitrage-bot"
set PYTHONIOENCODING=utf-8

:loop
echo [%date% %time%] Starting ws_trader.py...
python ws_trader.py
echo [%date% %time%] Bot exited with code %errorlevel%. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
