"""
热血青年交易所 - 统一入口
双击运行：主窗口(含仪表盘) + 系统托盘 + 交易引擎子线程

架构：
  QQQLiveTrader → 写 state.json / position_snapshot.json
  Dashboard     → 直读 JSON 文件显示
  无需 trader_web.Engine（已去掉）
"""
import os
import sys
import json
import time
import threading
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from datetime import datetime

if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='replace')

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)

from config_manager import ConfigManager, get_config
from settings_gui import open_settings, COLORS

# 仪表盘配色覆盖（Revolut Dark风格）
_DASH = {
    'bg': '#19191E', 'surface': '#222228', 'border': '#333338',
    'fg': '#F0F0F2', 'fg2': '#94969E', 'accent': '#5B8DEF',
    'green': '#00C48C', 'red': '#FF4757',
}


class KeyDialog:
    def __init__(self):
        self.result = None
        self.win = tk.Tk()
        self.win.title("🔑 API密钥配置")
        self.win.geometry("480x320")
        self.win.configure(bg=_DASH['bg'])
        self.win.resizable(False, False)
        self.win.update_idletasks()
        x = (self.win.winfo_screenwidth() - 480) // 2
        y = (self.win.winfo_screenheight() - 320) // 2
        self.win.geometry(f"+{x}+{y}")

        tk.Label(self.win, text="🔥 热血青年交易所",
                 bg=_DASH['bg'], fg=_DASH['accent'],
                 font=('Microsoft YaHei', 16, 'bold')).pack(pady=(16, 4))
        tk.Label(self.win, text="首次运行，请输入长桥API密钥",
                 bg=_DASH['bg'], fg=_DASH['fg'],
                 font=('Microsoft YaHei', 10)).pack(pady=(0, 12))

        frame = tk.Frame(self.win, bg=_DASH['bg'])
        frame.pack(padx=20, fill='x')
        self.fields = {}
        for key, label in [('LONGPORT_APP_KEY', 'App Key'),
                           ('LONGPORT_APP_SECRET', 'App Secret'),
                           ('LONGPORT_ACCESS_TOKEN', 'Access Token')]:
            tk.Label(frame, text=label, bg=_DASH['bg'], fg=_DASH['fg'],
                     font=('Microsoft YaHei', 10), width=14, anchor='e').pack(side='left', padx=4, pady=4)
            var = tk.StringVar()
            tk.Entry(frame, textvariable=var, width=36,
                     show='*' if 'SECRET' in key or 'TOKEN' in key else '',
                     bg=_DASH['surface'], fg=_DASH['fg'],
                     insertbackground=_DASH['fg'],
                     font=('Consolas', 10), relief='flat').pack(side='left', padx=4, pady=4)
            self.fields[key] = var
            frame = tk.Frame(self.win, bg=_DASH['bg'])
            frame.pack(padx=20, fill='x')

        btn_frame = tk.Frame(self.win, bg=_DASH['bg'])
        btn_frame.pack(pady=16)
        tk.Button(btn_frame, text="💾 保存并启动", bg=_DASH['green'], fg='#000',
                  font=('Microsoft YaHei', 10, 'bold'), relief='flat',
                  padx=20, pady=6, command=self._save).pack(side='left', padx=8)
        tk.Button(btn_frame, text="跳过", bg=_DASH['border'], fg=_DASH['fg'],
                  font=('Microsoft YaHei', 10), relief='flat',
                  padx=12, pady=6, command=self._skip).pack(side='left', padx=8)

    def _save(self):
        env = {}
        for key, var in self.fields.items():
            val = var.get().strip()
            if not val:
                messagebox.showwarning("缺少密钥", "请填写所有密钥字段")
                return
            env[key] = val
        ConfigManager.save_env(env)
        self.result = 'saved'
        self.win.destroy()

    def _skip(self):
        self.result = 'skip'
        self.win.destroy()

    def run(self):
        self.win.mainloop()
        return self.result


def create_tray_icon(app):
    try:
        import pystray
        from PIL import Image, ImageDraw
        def create_icon_image():
            img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([8, 8, 56, 56], fill=(102, 187, 106))
            draw.text((20, 16), "🔥", fill='white')
            return img
        def on_show(icon, item):
            icon.stop()
            app.win.after(0, app.show_window)
        def on_quit(icon, item):
            icon.stop()
            app.win.after(0, app.quit_app)
        menu = pystray.Menu(
            pystray.MenuItem('显示主窗口', on_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出', on_quit),
        )
        return pystray.Icon('QQQ Trading', create_icon_image(), '热血青年交易所', menu)
    except ImportError:
        return None


class TradingApp:
    def __init__(self):
        self.cfg = get_config()
        self.trader = None
        self.engine_thread = None
        self.running = False
        self.tray_icon = None

        self.win = tk.Tk()
        self.win.title("🔥 热血青年交易所 v5.0")
        self.win.geometry("500x280")
        self.win.configure(bg=_DASH['bg'])
        self.win.resizable(False, False)
        self.win.update_idletasks()
        x = (self.win.winfo_screenwidth() - 500) // 2
        y = (self.win.winfo_screenheight() - 280) // 2
        self.win.geometry(f"+{x}+{y}")
        self.win.protocol('WM_DELETE_WINDOW', self._minimize_to_tray)

        self._build_ui()

    def _build_ui(self):
        # 标题栏
        header = tk.Frame(self.win, bg=_DASH['accent'], height=44)
        header.pack(fill='x')
        header.pack_propagate(False)
        tk.Label(header, text="🔥 热血青年交易所",
                 bg=_DASH['accent'], fg='#000',
                 font=('Microsoft YaHei', 13, 'bold')).pack(side='left', padx=14, pady=6)
        tk.Label(header, text="QQQ 0DTE · v5.0",
                 bg=_DASH['accent'], fg='#1a1a2e',
                 font=('Microsoft YaHei', 9)).pack(side='right', padx=14)

        # 控制栏
        ctrl = tk.Frame(self.win, bg=_DASH['surface'])
        ctrl.pack(fill='x', padx=8, pady=6)

        self.engine_status = tk.StringVar(value="⚪ 引擎未启动")
        tk.Label(ctrl, textvariable=self.engine_status,
                 bg=_DASH['surface'], fg=_DASH['fg'],
                 font=('Microsoft YaHei', 10)).pack(side='left', padx=12)

        self.conn_status = tk.StringVar(value="⚪ 未连接")
        tk.Label(ctrl, textvariable=self.conn_status,
                 bg=_DASH['surface'], fg=_DASH['fg'],
                 font=('Microsoft YaHei', 10)).pack(side='left', padx=12)

        btn_kw = {'font': ('Microsoft YaHei', 9, 'bold'),
                  'relief': 'flat', 'cursor': 'hand2', 'padx': 12, 'pady': 3}

        self.btn_start = tk.Button(ctrl, text="▶ 启动",
                                   bg=_DASH['green'], fg='#000',
                                   command=self._start_engine, **btn_kw)
        self.btn_start.pack(side='right', padx=4)

        self.btn_stop = tk.Button(ctrl, text="⏹ 停止",
                                  bg=_DASH['red'], fg='#fff',
                                  command=self._stop_engine, state='disabled', **btn_kw)
        self.btn_stop.pack(side='right', padx=4)

        tk.Button(ctrl, text="⚙ 设置", bg=_DASH['border'], fg=_DASH['fg2'],
                  command=self._open_settings, **btn_kw).pack(side='right', padx=4)

        tk.Button(ctrl, text="✕ 退出", bg='#3a1a1a', fg=_DASH['red'],
                  command=self.quit_app, **btn_kw).pack(side='right', padx=4)

        # 仪表盘（Web版，自动打开浏览器）
        self.web_port = 8080
        try:
            import dashboard_web
            self.web_thread = dashboard_web.main(self.web_port)
            self.log(f"Web仪表盘已启动: http://127.0.0.1:{self.web_port}", 'success')
        except Exception as e:
            self.log(f"Web仪表盘启动失败: {e}", 'error')

        # 底部
        bottom = tk.Frame(self.win, bg=_DASH['surface'])
        bottom.pack(fill='x')
        self.time_label = tk.StringVar()
        tk.Label(bottom, textvariable=self.time_label,
                 bg=_DASH['surface'], fg=_DASH['fg2'],
                 font=('Consolas', 8)).pack(side='right', padx=10, pady=3)
        self._tick()

    def _tick(self):
        self.time_label.set(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.win.after(1000, self._tick)

    def log(self, msg, tag='info'):
        # 日志只输出到控制台（Web仪表盘有自己的事件系统）
        ts = datetime.now().strftime('%H:%M:%S')
        print(f'[{ts}] {msg}')

    def _start_engine(self):
        if self.running:
            return
        self.running = True
        self.btn_start.config(state='disabled', text='⏳ 启动中...')
        self.btn_stop.config(state='normal')
        self.engine_status.set("🟡 引擎启动中...")
        self.log("正在启动交易引擎...", 'info')

        self.engine_thread = threading.Thread(target=self._run_engine, daemon=True)
        self.engine_thread.start()

        self.win.after(5000, self._check_startup)

    def _run_engine(self):
        try:
            from live_trader import QQQLiveTrader, CONFIG as TRADER_CFG
            self.trader = QQQLiveTrader(TRADER_CFG)
            self.trader.start()
        except Exception as e:
            self.win.after(0, lambda: self.log(f"交易引擎异常: {e}", 'error'))
            self.win.after(0, lambda: self.engine_status.set("🔴 引擎异常"))

    def _check_startup(self):
        if not self.running:
            return
        alive = self.engine_thread and self.engine_thread.is_alive()
        if alive:
            self.engine_status.set("🟢 引擎运行中")
            self.conn_status.set("🟢 已连接")
            self.log("交易引擎已启动", 'success')
            self.btn_start.config(text="🟢 运行中")
        else:
            self.engine_status.set("🔴 引擎异常")
            self.log("交易引擎启动失败，请检查API密钥和网络", 'error')

    def _stop_engine(self):
        if not self.running:
            return
        self.running = False
        self.log("正在停止引擎...", 'warn')
        self.engine_status.set("🟡 停止中...")

        if self.trader:
            try:
                self.trader.stop()
            except:
                pass

        self.btn_start.config(state='normal', text='▶ 启动引擎')
        self.btn_stop.config(state='disabled')
        self.engine_status.set("⚪ 引擎已停止")
        self.conn_status.set("⚪ 未连接")
        self.log("引擎已停止", 'success')

    def _open_settings(self):
        def on_apply():
            self.cfg.load()
            self.log("⚙️ 配置已更新", 'success')
        open_settings(self.win, on_apply)

    def _minimize_to_tray(self):
        self.win.withdraw()
        if self.tray_icon:
            return
        self.tray_icon = create_tray_icon(self)
        if self.tray_icon:
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        else:
            self.win.iconify()

    def show_window(self):
        if self.tray_icon:
            self.tray_icon = None
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def quit_app(self):
        if self.running:
            self._stop_engine()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except:
                pass
        self.win.destroy()
        os._exit(0)  # 强制退出所有线程（包括Web服务器）

    def run(self):
        self.win.mainloop()


def check_first_run():
    from config_manager import ENV_FILE
    if not ENV_FILE.exists():
        return True
    env = ConfigManager.load_env()
    return not (env.get('LONGPORT_APP_KEY') and env.get('LONGPORT_ACCESS_TOKEN'))


def main():
    get_config()
    if check_first_run():
        d = KeyDialog()
        if d.run() == 'saved':
            for k, v in ConfigManager.load_env().items():
                os.environ[k] = v
    for k, v in ConfigManager.load_env().items():
        os.environ[k] = v

    app = TradingApp()
    app.log("🔥 热血青年交易所 v5.0 已启动", 'success')
    app.log("   2秒后自动启动引擎...", 'info')
    app.win.after(2000, app._start_engine)
    app.run()


if __name__ == '__main__':
    main()
