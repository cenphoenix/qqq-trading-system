#!/usr/bin/env python3
"""
天才浚浚交易所 - Web版入口（无GUI）
运行后启动：
1. 交易引擎（后台线程）
2. Web仪表盘（http://localhost:8080）
"""
import os
import sys
import threading
import time
import signal
import webbrowser
from pathlib import Path

print("run_web.py 开始执行", flush=True)

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)

os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')


def load_env():
    """加载环境变量"""
    print("load_env() 开始", flush=True)
    try:
        from config_manager import ConfigManager
        env_vars = ConfigManager.load_env()
        for k, v in env_vars.items():
            os.environ[k] = v
        print("load_env() 完成", flush=True)
    except Exception as e:
        print(f"load_env() 失败: {e}", flush=True)


def notify_telegram(msg):
    """发送Telegram通知"""
    try:
        import requests
        from config_manager import get_flat_config
        cfg = get_flat_config()
        tg = cfg.get('telegram', {})
        if not tg.get('enabled'):
            return
        bot_token = tg.get('bot_token', '')
        chat_id = tg.get('chat_id', '')
        if not bot_token or not chat_id:
            return
        requests.post(
            f'https://api.telegram.org/bot{bot_token}/sendMessage',
            json={'chat_id': chat_id, 'text': f"[QQQ Trader]\n{msg}"},
            timeout=10
        )
    except Exception:
        pass


def start_trader():
    """启动交易引擎"""
    print("start_trader() 开始", flush=True)
    try:
        from live_trader import main as trader_main
        print("live_trader导入成功", flush=True)
        trader_main()
    except Exception as e:
        print(f"start_trader() 失败: {e}", flush=True)
        import traceback
        traceback.print_exc()


def start_web():
    """启动v7 Web仪表盘"""
    print("start_web() 开始", flush=True)
    try:
        import dashboard_v7
        print("dashboard_v7导入成功", flush=True)
        dashboard_v7.run_dashboard("0.0.0.0", 8080)
    except Exception as e:
        print(f"start_web() 失败: {e}", flush=True)
        import traceback
        traceback.print_exc()


trader_started = False


def main():
    global trader_started

    print("=" * 50, flush=True)
    print("🔥 天才浚浚交易所 Web版 v7", flush=True)
    print("=" * 50, flush=True)

    load_env()

    # 启动Web仪表盘（阻塞）
    print("启动Web仪表盘线程...", flush=True)
    web_thread = threading.Thread(target=start_web, daemon=True)
    web_thread.start()

    time.sleep(1)
    print("🌐 Web仪表盘: http://localhost:8080", flush=True)
    print("📨 交易通知: Telegram (如已配置)", flush=True)
    print("-" * 50, flush=True)

    # 启动交易引擎
    print("🚀 启动交易引擎...", flush=True)
    trader_started = True
    start_trader()


def exit_handler(signum, frame):
    notify_telegram("🛑 系统关闭")
    print("\n👋 已发送关闭通知")


if __name__ == '__main__':
    print("进入main()", flush=True)
    main()
