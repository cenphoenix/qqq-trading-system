"""
热血青年交易所 - 设置界面
tkinter 原生 GUI，4个Tab：信号/风控/窗口/API
"""
import tkinter as tk
from tkinter import ttk, messagebox
from config_manager import ConfigManager, PARAM_TYPES, get_config


# 颜色主题
COLORS = {
    'bg': '#1e1e1e',
    'fg': '#e0e0e0',
    'accent': '#4fc3f7',
    'green': '#66bb6a',
    'red': '#ef5350',
    'orange': '#ffa726',
    'entry_bg': '#2d2d2d',
    'entry_fg': '#ffffff',
    'frame_bg': '#252525',
    'btn_bg': '#37474f',
    'btn_fg': '#ffffff',
}


class SettingsWindow:
    """设置窗口"""

    def __init__(self, parent=None):
        self.cfg = get_config()
        self.entries = {}     # key -> widget
        self.vars = {}        # key -> StringVar/IntVar/BooleanVar
        self.on_apply = None  # 应用回调

        self.win = tk.Toplevel(parent) if parent else tk.Tk()
        self.win.title("⚙️ 热血青年交易所 - 参数设置")
        self.win.geometry("720x620")
        self.win.configure(bg=COLORS['bg'])
        self.win.resizable(True, True)

        self._build_ui()
        self._load_values()

    def _build_ui(self):
        """构建UI"""
        # 顶部标题
        header = tk.Frame(self.win, bg=COLORS['accent'], height=36)
        header.pack(fill='x')
        header.pack_propagate(False)
        tk.Label(header, text="⚙️ 交易参数设置",
                 bg=COLORS['accent'], fg='#000000',
                 font=('Microsoft YaHei', 12, 'bold')).pack(pady=5)

        # Notebook (Tab)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TNotebook', background=COLORS['bg'])
        style.configure('TNotebook.Tab',
                        background=COLORS['frame_bg'],
                        foreground=COLORS['fg'],
                        padding=[16, 6],
                        font=('Microsoft YaHei', 10))
        style.map('TNotebook.Tab',
                   background=[('selected', COLORS['accent'])],
                   foreground=[('selected', '#000000')])

        notebook = ttk.Notebook(self.win)
        notebook.pack(fill='both', expand=True, padx=8, pady=8)

        # 5个Tab
        self.tab_signal = self._create_tab(notebook, "📊 信号参数", "signal")
        self.tab_risk = self._create_tab(notebook, "💰 资金风控", "risk")
        self.tab_trading = self._create_tab(notebook, "🌐 交易窗口", "trading")
        self.tab_feishu = self._create_tab(notebook, "🔑 API配置", "feishu")
        self.tab_telegram = self._create_tab(notebook, "📨 Telegram", "telegram")

        # 底部按钮栏
        btn_frame = tk.Frame(self.win, bg=COLORS['bg'])
        btn_frame.pack(fill='x', padx=8, pady=(0, 8))

        btn_style = {'font': ('Microsoft YaHei', 10),
                     'relief': 'flat', 'cursor': 'hand2',
                     'padx': 16, 'pady': 4}

        tk.Button(btn_frame, text="💾 保存配置", bg=COLORS['green'],
                  fg='#000000', command=self._save, **btn_style).pack(side='left', padx=4)

        tk.Button(btn_frame, text="🔄 重置默认", bg=COLORS['orange'],
                  fg='#000000', command=self._reset, **btn_style).pack(side='left', padx=4)

        tk.Button(btn_frame, text="✅ 保存并应用", bg=COLORS['accent'],
                  fg='#000000', command=self._apply, **btn_style).pack(side='right', padx=4)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(self.win, textvariable=self.status_var,
                 bg=COLORS['bg'], fg=COLORS['fg'],
                 font=('Microsoft YaHei', 8)).pack(fill='x', padx=8, pady=(0, 4))

    def _create_tab(self, notebook, title, group):
        """创建一个参数Tab"""
        frame = tk.Frame(notebook, bg=COLORS['bg'])
        notebook.add(frame, text=title)

        # 滚动区域
        canvas = tk.Canvas(frame, bg=COLORS['bg'], highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=COLORS['bg'])

        scroll_frame.bind('<Configure>',
                          lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=scroll_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        # 鼠标滚轮
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all('<MouseWheel>', _on_mousewheel)

        # 填充参数行
        row = 0
        for key, meta in PARAM_TYPES.items():
            g, k = key.split('.')
            if g != group:
                continue

            # 标签
            lbl_text = meta.get('label', k)
            tk.Label(scroll_frame, text=lbl_text, bg=COLORS['bg'], fg=COLORS['fg'],
                     font=('Microsoft YaHei', 10), anchor='e', width=18).grid(
                row=row, column=0, padx=(10, 6), pady=4, sticky='e')

            # 输入控件
            if meta['type'] == 'bool':
                var = tk.BooleanVar()
                widget = tk.Checkbutton(scroll_frame, variable=var,
                                        bg=COLORS['bg'], fg=COLORS['fg'],
                                        selectcolor=COLORS['entry_bg'],
                                        activebackground=COLORS['bg'],
                                        font=('Microsoft YaHei', 10))
                widget.grid(row=row, column=1, padx=6, pady=4, sticky='w')
            elif meta['type'] == 'time':
                var = tk.StringVar()
                widget = tk.Entry(scroll_frame, textvariable=var, width=12,
                                  bg=COLORS['entry_bg'], fg=COLORS['entry_fg'],
                                  insertbackground=COLORS['entry_fg'],
                                  font=('Consolas', 11), relief='flat')
                widget.grid(row=row, column=1, padx=6, pady=4, sticky='w')
                tk.Label(scroll_frame, text="HH:MM", bg=COLORS['bg'],
                         fg='#666666', font=('Microsoft YaHei', 8)).grid(
                    row=row, column=2, padx=4, sticky='w')
            elif meta['type'] == 'str':
                var = tk.StringVar()
                widget = tk.Entry(scroll_frame, textvariable=var, width=36,
                                  bg=COLORS['entry_bg'], fg=COLORS['entry_fg'],
                                  insertbackground=COLORS['entry_fg'],
                                  font=('Consolas', 11), relief='flat')
                widget.grid(row=row, column=1, columnspan=2, padx=6, pady=4, sticky='w')
            else:
                # int / float
                var = tk.StringVar()
                widget = tk.Entry(scroll_frame, textvariable=var, width=12,
                                  bg=COLORS['entry_bg'], fg=COLORS['entry_fg'],
                                  insertbackground=COLORS['entry_fg'],
                                  font=('Consolas', 11), relief='flat')
                widget.grid(row=row, column=1, padx=6, pady=4, sticky='w')

                # 范围提示
                if 'min' in meta and 'max' in meta:
                    range_text = f"[{meta['min']} ~ {meta['max']}]"
                    tk.Label(scroll_frame, text=range_text, bg=COLORS['bg'],
                             fg='#666666', font=('Microsoft YaHei', 8)).grid(
                        row=row, column=2, padx=4, sticky='w')

            self.entries[key] = widget
            self.vars[key] = var
            row += 1

        return frame

    def _load_values(self):
        """从配置加载当前值"""
        for key, var in self.vars.items():
            g, k = key.split('.')
            val = self.cfg.get(g, k)
            if val is None:
                continue
            meta = PARAM_TYPES[key]
            if meta['type'] == 'bool':
                var.set(bool(val))
            elif meta['type'] in ('int', 'float'):
                display = val
                if meta.get('display_pct') and isinstance(val, (int, float)):
                    display = round(val * 100, 2)
                var.set(str(display))
            else:
                var.set(str(val))

    def _collect_values(self):
        """从界面收集所有值"""
        groups = {}
        for key, var in self.vars.items():
            g, k = key.split('.')
            if g not in groups:
                groups[g] = {}
            meta = PARAM_TYPES[key]
            raw = var.get()

            if meta['type'] == 'bool':
                groups[g][k] = bool(raw)
            elif meta['type'] == 'int':
                try:
                    groups[g][k] = int(float(raw))
                except ValueError:
                    messagebox.showerror("输入错误", f"{meta['label']} 必须是整数")
                    return None
            elif meta['type'] == 'float':
                try:
                    val = float(raw)
                    if meta.get('display_pct'):
                        val = val / 100.0  # 百分比转小数
                    groups[g][k] = val
                except ValueError:
                    messagebox.showerror("输入错误", f"{meta['label']} 必须是数字")
                    return None
            else:
                groups[g][k] = raw

            # 范围验证
            if 'min' in meta and 'max' in meta:
                v = groups[g][k]
                lo, hi = meta['min'], meta['max']
                # 百分比字段已转回小数，用原始范围
                if meta.get('display_pct'):
                    lo, hi = meta['min'], meta['max']
                if not (lo <= v <= hi):
                    display_v = v * 100 if meta.get('display_pct') else v
                    messagebox.showwarning("参数超范围",
                        f"{meta['label']} = {display_v}，建议范围 [{lo} ~ {hi}]")

        return groups

    def _save(self):
        """保存配置"""
        groups = self._collect_values()
        if groups is None:
            return

        # 保留 signal.symbol 不变
        groups['signal']['symbol'] = self.cfg.get('signal', 'symbol', 'QQQ.US')

        for g, vals in groups.items():
            self.cfg.set_group(g, vals)
        self.cfg.save()
        self.status_var.set(f"✅ 配置已保存 {self.cfg.get('_last_modified')}")
        messagebox.showinfo("保存成功", "配置已保存到 settings.json\n引擎下次循环将自动读取新参数")

    def _reset(self):
        """重置为默认值"""
        if messagebox.askyesno("确认重置", "确定要恢复所有参数为默认值？"):
            self.cfg.reset_to_default()
            self._load_values()
            self.status_var.set("🔄 已重置为默认配置")

    def _apply(self):
        """保存并通知引擎重启"""
        self._save()
        if self.on_apply:
            self.on_apply()
        self.status_var.set("✅ 配置已保存并应用，引擎将在下次循环读取新参数")

    def run(self):
        """主循环（独立运行时）"""
        self.win.mainloop()


def open_settings(parent=None, on_apply=None):
    """打开设置窗口"""
    win = SettingsWindow(parent)
    win.on_apply = on_apply
    return win


if __name__ == '__main__':
    # 独立测试
    app = SettingsWindow()
    app.run()
