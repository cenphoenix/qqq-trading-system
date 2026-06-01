#!/usr/bin/env python3
"""
QQQ 0DTE v7 entrypoint.

Starts:
1. FastAPI dashboard on http://localhost:8080
2. live trading engine in the same process
"""
import os
import sys
import threading
import time
from pathlib import Path


print("run_web.py started", flush=True)

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def load_env():
    print("Loading env...", flush=True)
    try:
        from config_manager import ConfigManager
        env_vars = ConfigManager.load_env()
        for k, v in env_vars.items():
            os.environ[k] = v
        print("Env loaded", flush=True)
    except Exception as e:
        print(f"Env load failed: {e}", flush=True)


def notify_telegram(msg):
    try:
        import requests
        from config_manager import get_flat_config

        cfg = get_flat_config()
        tg = cfg.get("telegram", {})
        if not tg.get("enabled"):
            return
        bot_token = tg.get("bot_token", "")
        chat_id = tg.get("chat_id", "")
        if not bot_token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"[QQQ Trader]\n{msg}"},
            timeout=10,
        )
    except Exception:
        pass


def start_web():
    print("Starting dashboard...", flush=True)
    try:
        os.environ["QQQ_DASHBOARD_STARTED"] = "1"
        import dashboard_v7

        print("dashboard_v7 imported", flush=True)
        dashboard_v7.run_dashboard("0.0.0.0", 8080)
    except Exception as e:
        print(f"Dashboard failed: {e}", flush=True)
        import traceback

        traceback.print_exc()


def start_trader():
    print("Starting trader...", flush=True)
    try:
        from live_trader import main as trader_main

        print("live_trader imported", flush=True)
        trader_main()
    except Exception as e:
        print(f"Trader failed: {e}", flush=True)
        import traceback

        traceback.print_exc()


def main():
    print("=" * 50, flush=True)
    print("QQQ 0DTE Trading System v7", flush=True)
    print("=" * 50, flush=True)

    load_env()

    web_thread = threading.Thread(target=start_web, daemon=True)
    web_thread.start()
    os.environ["QQQ_DASHBOARD_STARTED"] = "1"

    time.sleep(1)
    print("Dashboard: http://localhost:8080", flush=True)
    print("Notifications: Telegram if configured", flush=True)
    print("-" * 50, flush=True)

    start_trader()


def exit_handler(signum=None, frame=None):
    notify_telegram("System stopped")
    print("\nSystem stopped", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        exit_handler()
