#!/usr/bin/env python3
"""
热血青年交易所 - Web版入口（无GUI）
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
    from config_manager import ConfigManager
    env_vars = ConfigManager.load_env()
    for k, v in env_vars.items():
        os.environ[k] = v


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
    from live_trader import main as trader_main
    trader_main()


def start_web():
    """启动Web仪表盘"""
    import trader_web
    trader_web.start_web(8080)


trader_started = False


def main():
    global trader_started

    print("=" * 50)
    print("🔥 热血青年交易所 Web版 v6.2")
    print("=" * 50)

    load_env()

    notify_telegram("🚀 系统启动")

    # 启动Web仪表盘（阻塞）
    web_thread = threading.Thread(target=start_web, daemon=True)
    web_thread.start()

    time.sleep(1)
    print("🌐 Web仪表盘: http://localhost:8080")
    print("📨 交易通知: Telegram (如已配置)")
    print("-" * 50)

    # 启动交易引擎
    print("🚀 启动交易引擎...")
    trader_started = True
    start_trader()


def exit_handler(signum, frame):
    notify_telegram("🛑 系统关闭")
    print("\n👋 已发送关闭通知")
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, exit_handler)
    signal.signal(signal.SIGTERM, exit_handler)
    main()