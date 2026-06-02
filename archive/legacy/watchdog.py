# -*- coding: utf-8 -*-
"""
QQQ 0DTE Live Trading Watchdog
- Auto-start live_trader.py
- Auto-restart on crash (max 5 times)
- Log all output
"""
import os, sys, time, signal, subprocess
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADER_SCRIPT = os.path.join(SCRIPT_DIR, "live_trader.py")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
MAX_RESTARTS = 5
RESTART_DELAY = 10

# 优先使用hermes venv的python
PYTHON_BIN = "/home/shanhaifeng/.hermes/hermes-agent/venv/bin/python"
if not os.path.exists(PYTHON_BIN):
    PYTHON_BIN = sys.executable

os.makedirs(LOG_DIR, exist_ok=True)
running = True

def signal_handler(sig, frame):
    global running
    running = False
    print("\nStopping...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

print("=" * 50)
print("QQQ 0DTE Watchdog")
print(f"Script: {TRADER_SCRIPT}")
print(f"Logs: {LOG_DIR}")
print(f"Max restarts: {MAX_RESTARTS}")
print("=" * 50)

restart_count = 0

while running and restart_count < MAX_RESTARTS:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"trader_{ts}.log")

    print(f"\nStarting trader (attempt #{restart_count+1}) at {datetime.now().strftime('%H:%M:%S')}")
    print(f"Log: {log_file}")

    try:
        with open(log_file, "w", encoding="utf-8") as log:
            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'
            proc = subprocess.Popen(
                [PYTHON_BIN, '-u', TRADER_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=SCRIPT_DIR,
                env=env,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )

            for line in proc.stdout:
                print(line, end="")
                log.write(line)
                log.flush()

            proc.wait()
            exit_code = proc.returncode

    except Exception as e:
        exit_code = -1
        print(f"Exception: {e}")

    if exit_code == 0:
        print("Trader exited normally")
        break
    else:
        restart_count += 1
        if restart_count < MAX_RESTARTS and running:
            print(f"Trader crashed (code={exit_code}), restarting in {RESTART_DELAY}s...")
            time.sleep(RESTART_DELAY)
        else:
            print(f"Max restarts ({MAX_RESTARTS}) reached, stopping watchdog")

print("\nWatchdog stopped")
