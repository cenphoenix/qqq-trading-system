"""
v7 Dashboard启动脚本
替换现有run_web.py
"""
import sys
import os
import threading
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard_v7 import run_dashboard, set_signal_manager
from v7_integration import V7Integration


def start_dashboard_with_trader(trader_instance, host="0.0.0.0", port=8080):
    """
    启动dashboard并绑定到trader实例
    
    Args:
        trader_instance: QQQLiveTrader实例
        host: 监听地址
        port: 监听端口
    """
    # 设置信号管理器
    set_signal_manager(trader_instance.v7.signal_manager)
    
    # 启动dashboard（在新线程中）
    def run():
        run_dashboard(host, port)
        
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    
    return thread


if __name__ == "__main__":
    # 独立运行dashboard（用于测试）
    print("🚀 启动v7 Dashboard (测试模式)")
    print("📊 访问 http://localhost:8080")
    run_dashboard()
