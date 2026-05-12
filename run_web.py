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


def start_trader():
    """启动交易引擎"""
    from live_trader import main as trader_main
    trader_main()


def start_web():
    """启动Web仪表盘"""
    import dashboard_web
    dashboard_web.main(8080)


def main():
    print("=" * 50)
    print("🔥 热血青年交易所 Web版 v6.2")
    print("=" * 50)

    load_env()

    # 启动Web仪表盘（阻塞）
    web_thread = threading.Thread(target=start_web, daemon=True)
    web_thread.start()

    time.sleep(1)
    print("🌐 Web仪表盘: http://localhost:8080")
    print("📨 交易通知: Telegram (如已配置)")
    print("-" * 50)

    # 启动交易引擎
    print("🚀 启动交易引擎...")
    start_trader()


if __name__ == '__main__':
    main()