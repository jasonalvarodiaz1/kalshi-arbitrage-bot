"""Auto-restart wrapper for ws_trader.py.

Keeps the bot alive by restarting it if it exits for any reason.
Logs restart events to run_bot.log.
Uses PID lock file to prevent concurrent instances.
"""
import subprocess
import sys
import time
import datetime
import os
from pathlib import Path

LOG_FILE = "run_bot.log"
BOT_SCRIPT = "ws_trader.py"
PID_FILE = "ws_trader.pid"
MIN_RESTART_INTERVAL = 10  # Don't restart faster than every 10 seconds
MAX_FAST_RESTARTS = 5       # If it restarts 5 times in under 60s, wait 5 min


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    log("=== Bot auto-restart wrapper started ===")

    fast_restart_times = []

    while True:
        start_time = time.time()

        # Clean stale PID lock file before starting
        pid_path = Path(os.path.dirname(os.path.abspath(__file__))) / PID_FILE
        if pid_path.exists():
            try:
                pid_path.unlink()
                log(f"Cleaned stale PID file")
            except Exception:
                pass

        log(f"Starting {BOT_SCRIPT}...")

        try:
            proc = subprocess.run(
                [sys.executable, BOT_SCRIPT],
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            exit_code = proc.returncode
        except Exception as e:
            log(f"Failed to start bot: {e}")
            exit_code = -1

        elapsed = time.time() - start_time
        log(f"Bot exited with code {exit_code} after {elapsed:.0f}s")

        # Track fast restarts
        now = time.time()
        fast_restart_times.append(now)
        # Keep only last 60 seconds
        fast_restart_times = [t for t in fast_restart_times if now - t < 60]

        if len(fast_restart_times) >= MAX_FAST_RESTARTS:
            log(f"Too many fast restarts ({len(fast_restart_times)} in 60s) — cooling down 5 min")
            time.sleep(300)
            fast_restart_times.clear()
        elif elapsed < MIN_RESTART_INTERVAL:
            wait = MIN_RESTART_INTERVAL - elapsed
            log(f"Restarting in {wait:.0f}s (too fast)...")
            time.sleep(wait)
        else:
            log("Restarting in 3s...")
            time.sleep(3)


if __name__ == "__main__":
    main()
