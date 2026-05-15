"""
热血青年交易所 - 仪表盘 v3
参考 Revolut 金融风格：深色、扁平、清晰层次
"""
import os
import sys
import json
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent

# ===== 配色（Revolut Dark） =====
BG       = '#19191E'   # 主背景
SURFACE  = '#222228'   # 卡片
SURFACE2 = '#1A1A1F'   # 深色区
BORDER   = '#333338'   # 边线
FG       = '#F0F0F2'   # 主文字
FG2      = '#94969E'   # 次文字
FG3      = '#5C5E66'   # 最暗文字
GREEN    = '#00C48C'   # 盈利/做多
GREEN_BG = '#00C48C18' # 盈利背景
RED      = '#FF4757'   # 亏损/做空
RED_BG   = '#FF475718' # 亏损背景
BLUE     = '#5B8DEF'   # 强调
ORANGE   = '#FFA502'   # 警告
CYAN     = '#00D2D3'   # 信息
PURPLE   = '#A55EEA'   # 引擎

# ===== 字体 =====
FONT_TITLE = ('Microsoft YaHei', 11, 'bold')
FONT_LABEL = ('Microsoft YaHei', 9)
FONT_BODY  = ('Microsoft YaHei', 10)
FONT_BOLD  = ('Microsoft YaHei', 10, 'bold')
FONT_BIG   = ('Microsoft YaHei', 22, 'bold')
FONT_MONO  = ('Consolas', 10)
FONT_MONO_S= ('Consolas', 9)
FONT_SMALL = ('Consolas', 8)


def _card(parent, title, row, col, colspan=1):
    """创建卡片"""
    f = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
    f.grid(row=row, column=col, columnspan=colspan, sticky='nsew', padx=4, pady=4)
    # 标题
    tk.Label(f, text=title, bg=SURFACE, fg=FG2,
             font=FONT_LABEL, anchor='w').pack(fill='x', padx=12, pady=(8, 0))
    tk.Frame(f, bg=BORDER, height=1).pack(fill='x', padx=12, pady=(4, 6))
    return f


def _row(parent, label, bg=None):
    """卡片内一行：label + value"""
    bg = bg or SURFACE
    r = tk.Frame(parent, bg=bg)
    r.pack(fill='x', padx=12, pady=2)
    tk.Label(r, text=label, bg=bg, fg=FG2, font=FONT_LABEL,
             width=9, anchor='w').pack(side='left')
    v = tk.Label(r, text='--', bg=bg, fg=FG, font=FONT_MONO, anchor='e')
    v.pack(side='right', fill='x', expand=True)
    return v


class Dashboard(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.engine = None
        self._job = None
        self._prev_filter = {}
        self._prev_event_key = ''
        self._build()

    def set_engine(self, engine):
        self.engine = engine

    def _build(self):
        # 可滚动容器
        canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient='vertical', command=canvas.yview)
        self._inner = tk.Frame(canvas, bg=BG)
        self._inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=self._inner, anchor='nw')
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), 'units'))
        p = self._inner

        self._v = {}

        # ===== 第1行：4卡片 2×2 =====
        g1 = tk.Frame(p, bg=BG)
        g1.pack(fill='x', padx=8, pady=(8, 0))
        g1.columnconfigure(0, weight=1, uniform='a')
        g1.columnconfigure(1, weight=1, uniform='a')
        g1.columnconfigure(2, weight=1, uniform='a')
        g1.columnconfigure(3, weight=1, uniform='a')

        c1 = _card(g1, '📊 当日', 0, 0)
        self._v['pnl']     = _row(c1, '盈亏')
        self._v['trades']  = _row(c1, '交易')
        self._v['winrate'] = _row(c1, '胜率')
        self._v['holding'] = _row(c1, '持仓')

        c2 = _card(g1, '📈 行情', 0, 1)
        self._v['qqq'] = _row(c2, 'QQQ')
        self._v['chg'] = _row(c2, '涨跌')
        self._v['vol'] = _row(c2, '成交')

        c3 = _card(g1, '💰 资金', 0, 2)
        self._v['equity'] = _row(c3, '总资产')
        self._v['cash']   = _row(c3, '现金')
        self._v['power']  = _row(c3, '购买力')

        c4 = _card(g1, '⚙️ 系统', 0, 3)
        self._v['engine'] = _row(c4, '引擎')
        self._v['conn']   = _row(c4, '连接')
        self._v['uptime'] = _row(c4, '运行')

        # ===== 第2行：信号 + 过滤器 =====
        g2 = tk.Frame(p, bg=BG)
        g2.pack(fill='x', padx=8, pady=4)
        g2.columnconfigure(0, weight=2, uniform='b')
        g2.columnconfigure(1, weight=3, uniform='b')

        # 信号
        sig = _card(g2, '🎯 信号', 0, 0)
        self._sig_icon = tk.Label(sig, text='⏳', bg=SURFACE, font=('Segoe UI Emoji', 32))
        self._sig_icon.pack(pady=(6, 0))
        self._sig_dir = tk.Label(sig, text='无信号', bg=SURFACE, fg=FG3,
                                  font=('Microsoft YaHei', 16, 'bold'))
        self._sig_dir.pack()
        self._sig_price = tk.Label(sig, text='', bg=SURFACE, fg=FG, font=FONT_MONO)
        self._sig_price.pack()
        self._sig_reason = tk.Label(sig, text='', bg=SURFACE, fg=FG3,
                                     font=FONT_LABEL, wraplength=220)
        self._sig_reason.pack(pady=(0, 8))

        # 过滤器
        filt = _card(g2, '🔍 过滤器', 0, 1)
        self._fw = {}
        for fname, flabel in [('sma20','SMA20'),('volume','量能'),('momentum','动量'),('body','K线实体')]:
            r = tk.Frame(filt, bg=SURFACE)
            r.pack(fill='x', padx=12, pady=2)
            dot = tk.Label(r, text='●', bg=SURFACE, fg=FG3, font=('Consolas', 8))
            dot.pack(side='left', padx=(0,6))
            tk.Label(r, text=flabel, bg=SURFACE, fg=FG2, font=FONT_LABEL,
                     width=7, anchor='w').pack(side='left')
            val = tk.Label(r, text='--', bg=SURFACE, fg=FG, font=FONT_MONO_S, width=10, anchor='w')
            val.pack(side='left', padx=(8,0))
            detail = tk.Label(r, text='', bg=SURFACE, fg=FG3, font=FONT_SMALL)
            detail.pack(side='right')
            self._fw[fname] = {'dot': dot, 'val': val, 'detail': detail}

        # ===== 第3行：持仓 + 交易记录 =====
        g3 = tk.Frame(p, bg=BG)
        g3.pack(fill='x', padx=8, pady=4)
        g3.columnconfigure(0, weight=1, uniform='c')
        g3.columnconfigure(1, weight=1, uniform='c')

        style = ttk.Style()
        style.theme_use('clam')
        style.configure('D.Treeview', background=SURFACE2, foreground=FG,
                        fieldbackground=SURFACE2, font=FONT_MONO_S, rowheight=24)
        style.configure('D.Treeview.Heading', background=SURFACE, foreground=FG2,
                        font=('Microsoft YaHei', 8, 'bold'))
        style.map('D.Treeview', background=[('selected', BLUE)])

        # 持仓
        pos = _card(g3, '📋 持仓', 0, 0)
        self._pos_tree = ttk.Treeview(pos, columns=('sym','qty','cost','cur','pnl','pct'),
                                       show='headings', height=4, style='D.Treeview')
        for c,n,w in [('sym','标的',110),('qty','数量',50),('cost','成本',70),
                       ('cur','现价',70),('pnl','盈亏',80),('pct','盈亏%',65)]:
            self._pos_tree.heading(c, text=n)
            self._pos_tree.column(c, width=w, anchor='center')
        self._pos_tree.pack(fill='x', padx=4, pady=4)

        # 交易记录
        trd = _card(g3, '📝 交易', 0, 1)
        self._trd_tree = ttk.Treeview(trd, columns=('time','dir','opt','entry','exit','qty','pnl'),
                                       show='headings', height=4, style='D.Treeview')
        for c,n,w in [('time','时间',52),('dir','方向',42),('opt','期权',100),
                       ('entry','开仓',62),('exit','平仓',62),('qty','数量',42),('pnl','盈亏',72)]:
            self._trd_tree.heading(c, text=n)
            self._trd_tree.column(c, width=w, anchor='center')
        self._trd_tree.pack(fill='x', padx=4, pady=4)

        # ===== 第4行：实时日志 =====
        log = tk.Frame(p, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        log.pack(fill='both', expand=True, padx=8, pady=(4, 8))
        tk.Label(log, text='📋 实时事件（信号/过滤器/交易）', bg=SURFACE, fg=FG2,
                 font=FONT_LABEL, anchor='w').pack(fill='x', padx=12, pady=(8, 0))
        tk.Frame(log, bg=BORDER, height=1).pack(fill='x', padx=12, pady=(4, 6))

        self._elog = tk.Text(log, bg=SURFACE2, fg=FG, font=FONT_MONO_S,
                              relief='flat', height=7, state='disabled', wrap='word')
        self._elog.pack(fill='both', expand=True, padx=4, pady=(0, 4))
        sb = tk.Scrollbar(self._elog, command=self._elog.yview)
        sb.pack(side='right', fill='y')
        self._elog.config(yscrollcommand=sb.set)
        self._elog.tag_configure('sig',    foreground=GREEN)
        self._elog.tag_configure('sig_r',  foreground=RED)
        self._elog.tag_configure('trade',  foreground=CYAN)
        self._elog.tag_configure('ok',     foreground=GREEN)
        self._elog.tag_configure('no',     foreground=RED)
        self._elog.tag_configure('warn',   foreground=ORANGE)
        self._elog.tag_configure('engine', foreground=PURPLE)
        self._elog.tag_configure('info',   foreground=FG3)

    # ===== 日志 =====
    def _log(self, msg, tag='info'):
        ts = datetime.now().strftime('%H:%M:%S')
        self._elog.config(state='normal')
        self._elog.insert('end', f'[{ts}] {msg}\n', tag)
        n = int(self._elog.index('end-1c').split('.')[0])
        if n > 200:
            self._elog.delete('1.0', f'{n-200}.0')
        self._elog.see('end')
        self._elog.config(state='disabled')

    # ===== 更新 =====
    def start_update(self, sec=5):
        self._update()
        self._job = self.after(sec * 1000, self.start_update, sec)

    def stop_update(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None

    def _read_files(self):
        """直读JSON文件"""
        try:
            sd = str(BASE_DIR)
            sf = os.path.join(sd, 'state.json')
            if not os.path.exists(sf):
                return None
            with open(sf, encoding='utf-8') as f:
                sh = json.load(f)

            positions = []
            pf = os.path.join(sd, 'position_snapshot.json')
            if os.path.exists(pf):
                with open(pf, encoding='utf-8') as f:
                    raw = json.load(f)
                positions = raw if isinstance(raw, list) else raw.get('positions', [raw])

            trades = []
            for t in sh.get('trades_today', []):
                trades.append({
                    'time': t.get('time', '--'),
                    'dir': '做多' if t.get('dir') == 'call' else '做空',
                    'opt': t.get('opt_symbol', '--'),
                    'ep': f"${t.get('entry_price', 0):.2f}",
                    'exit_price': f"${t.get('exit_price', 0):.2f}" if t.get('exit_price') else '',
                    'qty': t.get('contracts', 0),
                    'pnl_usd': t.get('pnl_usd', 0),
                })

            # 读长桥订单补充
            lf = os.path.join(sd, 'longbridge_orders.json')
            if os.path.exists(lf):
                with open(lf, encoding='utf-8') as f:
                    lb = json.load(f)
                for o in lb.get('orders', []):
                    eq = float(o.get('executed_qty', 0))
                    ep = float(o.get('executed_price', 0))
                    if eq > 0 and ep > 0:
                        trades.append({
                            'time': (o.get('updated_at', '')[-8:]) if o.get('updated_at') else '--',
                            'dir': '做多' if o.get('side') == '买入' else '做空',
                            'opt': o.get('symbol', '--'),
                            'ep': f'${ep:.2f}',
                            'exit_price': '',
                            'qty': int(eq),
                            'pnl_usd': 0,
                        })

            price = sh.get('current_price', 0)
            sig_d = sh.get('current_signal')
            sig = {'dir': '无信号', 'price': '--', 'reason': '--'}
            if sig_d:
                sig = {
                    'dir': '🟢做多' if sig_d.get('dir') == 'call' else '🔴做空',
                    'price': f"${sig_d.get('price', 0):.2f}",
                    'reason': sig_d.get('reason', '--'),
                }

            return {
                'connected': sh.get('connected', False),
                'running': sh.get('running', False),
                'quote': {'price': price, 'pct': 0, 'vol': sh.get('candle_count', 0)},
                'account': {},
                'positions': positions,
                'signal': sig,
                'filters': sh.get('filter_status', {}),
                'trades': trades,
                'events': sh.get('events', []),
                'daily': {
                    'pnl': sh.get('daily_pnl', 0),
                    'count': len(trades),
                    'holding': len(positions),
                },
                '_start_time': sh.get('updated', ''),
            }
        except Exception:
            return None

    def _update(self):
        state = None
        if self.engine:
            try:
                state = self.engine.get_state()
            except Exception:
                pass
        if not state:
            state = self._read_files()
        if not state:
            return

        # ----- 当日 -----
        d = state.get('daily', {})
        pnl = d.get('pnl', 0)
        self._set('pnl', f'${pnl:+,.2f}', GREEN if pnl >= 0 else RED)
        trades = state.get('trades', [])
        self._set('trades', str(d.get('count', len(trades))))
        closed = [t for t in trades if not t.get('active', True)]
        wins = sum(1 for t in closed if t.get('pnl_usd', 0) > 0)
        wr = (wins / len(closed) * 100) if closed else 0
        self._set('winrate', f'{wr:.0f}%', GREEN if wr >= 50 else RED)
        self._set('holding', str(d.get('holding', 0)))

        # ----- 行情 -----
        q = state.get('quote', {})
        p = q.get('price', 0)
        self._set('qqq', f'${p:.2f}' if p else '--')
        chg = q.get('pct', 0)
        self._set('chg', f'{chg:+.2f}%', GREEN if chg >= 0 else RED)
        v = q.get('vol', 0)
        self._set('vol', f'{v/1e6:.1f}M' if v >= 1e6 else (f'{v/1e3:.0f}K' if v >= 1e3 else str(v) if v else '--'))

        # ----- 资金 -----
        a = state.get('account', {})
        self._set('equity', f'${a.get("net", 0):,.2f}')
        self._set('cash', f'${a.get("cash", 0):,.2f}')
        self._set('power', f'${a.get("power", 0):,.2f}')

        # ----- 系统 -----
        running = state.get('running', False)
        self._set('engine', '运行中' if running else '已停止', GREEN if running else RED)
        conn = state.get('connected', False)
        self._set('conn', '已连接' if conn else '未连接', GREEN if conn else ORANGE)
        self._set('uptime', state.get('_start_time', '--'))

        # ----- 信号 -----
        sig = state.get('signal', {})
        sd = sig.get('dir', '无信号')
        if '做多' in sd:
            self._sig_icon.config(text='🟢')
            self._sig_dir.config(text=sd, fg=GREEN)
        elif '做空' in sd:
            self._sig_icon.config(text='🔴')
            self._sig_dir.config(text=sd, fg=RED)
        else:
            self._sig_icon.config(text='⏳')
            self._sig_dir.config(text='无信号', fg=FG3)
        self._sig_price.config(text=sig.get('price', ''))
        self._sig_reason.config(text=sig.get('reason', ''))

        # ----- 过滤器 -----
        fs = state.get('filters', {})
        for fn in ['sma20', 'volume', 'momentum', 'body']:
            f = fs.get(fn, {})
            ok = f.get('ok')
            val = f.get('val', '--')
            det = f.get('detail', '')
            w = self._fw[fn]
            if ok is True:
                w['dot'].config(fg=GREEN)
                w['val'].config(text=val, fg=GREEN)
            elif ok is False:
                w['dot'].config(fg=RED)
                w['val'].config(text=val, fg=RED)
            else:
                w['dot'].config(fg=FG3)
                w['val'].config(text=val, fg=FG3)
            w['detail'].config(text=det[:30] if det else '')

        # ----- 持仓表 -----
        self._pos_tree.delete(*self._pos_tree.get_children())
        for pos in state.get('positions', []):
            pnl_s = pos.get('pnl', '0')
            pct_s = pos.get('pct', '0%')
            up = '+' in str(pnl_s) or ('-' not in str(pnl_s))
            self._pos_tree.insert('', 'end', values=(
                pos.get('sym', '--'), pos.get('qty', 0),
                pos.get('cost', '--'), pos.get('cur', '--'),
                pnl_s, pct_s), tags=('w' if up else 'l',))
        self._pos_tree.tag_configure('w', foreground=GREEN)
        self._pos_tree.tag_configure('l', foreground=RED)

        # ----- 交易表 -----
        self._trd_tree.delete(*self._trd_tree.get_children())
        for t in reversed(trades[-15:]):
            pu = t.get('pnl_usd', 0)
            self._trd_tree.insert('', 'end', values=(
                t.get('time', '--'), t.get('dir', '--'), t.get('opt', '--'),
                t.get('ep', '--'), t.get('exit_price', '--'),
                t.get('qty', 0), f'${pu:+,.2f}' if pu else '--'),
                tags=('w' if pu > 0 else ('l' if pu < 0 else '',)))
        self._trd_tree.tag_configure('w', foreground=GREEN)
        self._trd_tree.tag_configure('l', foreground=RED)

        # ----- 实时日志 -----
        self._check_events(state, fs)

    def _set(self, k, txt, color=None):
        w = self._v.get(k)
        if w:
            w.config(text=txt)
            if color:
                w.config(fg=color)
            else:
                w.config(fg=FG)

    def _check_events(self, state, filters):
        # 事件（来自live_trader）
        for evt in state.get('events', []):
            key = f"{evt.get('time','')}|{evt.get('msg','')}"
            if key != self._prev_event_key:
                self._prev_event_key = key
                tag_map = {'signal': 'sig', 'trade': 'trade', 'error': 'no',
                           'engine': 'engine', 'info': 'info'}
                t = tag_map.get(evt.get('tag', 'info'), 'info')
                if t == 'sig' and '做空' in evt.get('msg', ''):
                    t = 'sig_r'
                self._log(evt.get('msg', ''), t)

        # 过滤器变化
        for fn, fl in [('sma20','SMA20'),('volume','量能'),('momentum','动量'),('body','K线实体')]:
            f = filters.get(fn, {})
            ok = f.get('ok')
            prev = self._prev_filter.get(fn)
            if ok != prev:
                self._prev_filter[fn] = ok
                val = f.get('val', '')
                if ok is True:
                    self._log(f'✅ {fl}: 通过 ({val})', 'ok')
                elif ok is False:
                    self._log(f'❌ {fl}: 不通过 ({val})', 'no')


def main():
    root = tk.Tk()
    root.title('仪表盘')
    root.geometry('1100x780')
    root.configure(bg=BG)
    d = Dashboard(root)
    d.pack(fill='both', expand=True)
    d.start_update(3)
    root.mainloop()

if __name__ == '__main__':
    main()
