"""Telegram HTML formatting separated from the live trading engine."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any


class TraderMessageFormatter:
    def __init__(self, trader: Any, timezone: tzinfo) -> None:
        self.trader = trader
        self.timezone = timezone

    def day_market(self, source: dict | None = None) -> str:
        regime = {}
        if isinstance(source, dict):
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            raw = metadata.get("day_market_regime")
            if isinstance(raw, dict):
                regime = raw
            else:
                regime = {"type": source.get("day_market_regime", ""), "label": source.get("day_market_label", "")}
        if not regime:
            current = getattr(self.trader, "day_market_regime", {})
            regime = current if isinstance(current, dict) else {}
        label = regime.get("label") or regime.get("type") or "--"
        direction = {"call": "偏多", "put": "偏空"}.get(regime.get("direction"), "中性")
        reason = regime.get("reason") or ""
        detail = f"<code>{reason}</code>\n" if reason else ""
        return f"当日行情 <b>{label}</b> ({direction})\n{detail}"

    def _stats(self) -> tuple[int, int, float]:
        closed = [trade for trade in self.trader.trades_today if trade.get("win") is not None]
        wins = sum(1 for trade in closed if trade.get("win"))
        rate = wins / len(closed) * 100 if closed else 0.0
        return len(closed), wins, rate

    def entry(self, sig, opt_symbol, price, contracts, qty, order_id) -> str:
        direction = "做多 CALL" if sig["dir"] == "call" else "做空 PUT"
        icon = "🟢" if sig["dir"] == "call" else "🔴"
        entry_opt = self.trader.position.get("entry_opt_price", 0) if self.trader.position else 0
        total, _, rate = self._stats()
        return (
            f"<b>🎯 开仓 #{len(self.trader.trades_today)}</b>\n───────────\n"
            f"{icon} <b>{direction}</b>\n<code>{opt_symbol}</code>\n───────────\n"
            f"正股 <b>${price:.2f}</b> | 期权 <b>${entry_opt:.2f}</b>\n数量 <b>{contracts}</b>张 ({qty}股)\n"
            f"策略行情 <b>{sig.get('regime', '--')}</b>\n{self.day_market(sig)}理由 {sig.get('reason', '--')}\n"
            f"订单 {order_id}\n───────────\n📈 今日统计\n交易 <b>{total}</b>笔 | 胜率<b>{rate:.0f}%</b> | "
            f"盈亏<b>${self.trader.daily_pnl:+,.2f}</b>\n🔥 连胜{self.trader.max_consecutive_wins} | ❄️ 连亏{self.trader.max_consecutive_losses}"
        )

    def exit(self, pos, reason, entry_opt, exit_opt, pnl_pct, pnl_usd, order_id="--") -> str:
        direction = "CALL" if pos.get("dir") == "call" else "PUT"
        icon = "🟢" if pos.get("dir") == "call" else "🔴"
        result_icon = "✅" if pnl_pct > 0 else "❌"
        label = "盈利" if pnl_pct > 0 else "亏损"
        total, _, rate = self._stats()
        return (
            f"<b>🏁 平仓 #{len(self.trader.trades_today)}</b>\n───────────\n"
            f"{icon} <b>{direction}</b> <code>{pos.get('opt_symbol', '')}</code>\n原因 <b>{reason}</b>\n───────────\n"
            f"入场 ${entry_opt:.2f} → 平仓 ${exit_opt:.2f}\n{result_icon} {label} <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+,.2f})\n"
            f"{self.day_market(pos)}订单 {order_id}\n───────────\n📈 今日统计\n"
            f"交易 <b>{total}</b>笔 | 胜率<b>{rate:.0f}%</b> | 盈亏<b>${self.trader.daily_pnl:+,.2f}</b>\n"
            f"🔥 连胜{self.trader.max_consecutive_wins} | ❄️ 连亏{self.trader.max_consecutive_losses}"
        )

    def partial(self, pos, reason, entry_opt, exit_opt, half, remaining, pnl_pct, pnl_usd) -> str:
        direction = "CALL" if pos.get("dir") == "call" else "PUT"
        icon = "🟢" if pos.get("dir") == "call" else "🔴"
        result_icon = "✅" if pnl_pct > 0 else "❌"
        return (
            f"<b>✂️ 部分平仓</b>\n───────────\n{icon} <b>{direction}</b> <code>{pos.get('opt_symbol', '')}</code>\n"
            f"原因 <b>{reason}</b>\n───────────\n入场 ${entry_opt:.2f} → 平仓 ${exit_opt:.2f}\n"
            f"{result_icon} <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+,.2f})\n{self.day_market(pos)}"
            f"平掉 <b>{half}</b>张 | 剩余 <b>{remaining}</b>张"
        )

    @staticmethod
    def alert(level, loss_pct, threshold) -> str:
        icons = {1: "⚠️", 2: "🔶", 3: "🔴"}
        labels = {1: "警告", 2: "保守", 3: "熔断"}
        actions = " 仓位减半" + (" | 只做trending" if level >= 2 else "") + (" | 停止所有交易" if level >= 3 else "")
        return f"<b>{icons.get(level, '⚠️')} 亏损{labels.get(level, '通知')}</b>\n───────────\n当前亏损 <b>{loss_pct:.1f}%</b> (阈值 {threshold:.0f}%)\n{actions}"

    @staticmethod
    def system(event_type, **kwargs) -> str:
        if event_type == "exit":
            return f"<b>⚠️ 系统退出</b>\n───────────\n原因 <b>{kwargs.get('sig_name', '')}</b>\n时间 {kwargs.get('time', '')}\n今日交易 <b>{kwargs.get('trades', 0)}</b>笔\n盈亏 <b>{kwargs.get('pnl', 0):+,.2f}</b>"
        if event_type == "crash":
            return f"<b>❌ 系统异常</b>\n───────────\n时间 {kwargs.get('time', '')}\n错误 <code>{kwargs.get('error', '')}</code>"
        if event_type == "cancel":
            return f"<b>⏰ 订单超时取消</b>\n───────────\n期权 <code>{kwargs.get('symbol', '')}</code>"
        return ""

    def startup(self) -> str:
        return (
            f"<b>🚀 系统启动</b>\n───────────\n版本 <code>v7 Multi-Engine</code>\n"
            f"时间 <code>{datetime.now(self.timezone).strftime('%Y-%m-%d %H:%M ET')}</code>\n"
            f"账户 <b>${self.trader.actual_capital:,.2f}</b>\n昨日盈亏 <b>${self.trader.yesterday_pnl:+,.2f}</b> "
            f"({self.trader.yesterday_trades}笔, 胜率{self.trader.yesterday_wr:.0f}%)"
        )

    def shutdown(self, reason="未知") -> str:
        runtime = datetime.now(self.timezone) - self.trader.start_time
        total, wins, _ = self._stats()
        return (
            f"<b>⏹️ 系统停止</b>\n───────────\n原因 <b>{reason}</b>\n"
            f"运行时长 <b>{int(runtime.total_seconds() // 3600)}h {int(runtime.total_seconds() % 3600 // 60)}m</b>\n"
            f"今日交易 <b>{total}</b>笔 | 盈利<b>{wins}</b> | 亏损<b>{total - wins}</b>\n盈亏 <b>${self.trader.daily_pnl:+,.2f}</b>"
        )

    def period_summary(self, period: str) -> str:
        try:
            if period == "day":
                self.trader._save_daily_records()
            from review_summary import build_review_summary
            return build_review_summary(period, datetime.now(self.timezone).strftime("%Y-%m-%d")).get("telegram_html", "")
        except Exception as error:
            return f"<b>{period}复盘生成失败</b>\n<code>{str(error)[:180]}</code>"

    def network(self, error_msg, retry_count=0) -> str:
        return f"<b>🌐 网络异常</b>\n───────────\n错误 <code>{error_msg[:100]}</code>\n重试次数 <b>{retry_count}</b>\n时间 {datetime.now(self.timezone).strftime('%H:%M:%S ET')}\n───────────\n系统将自动重连，请关注后续通知"

    def rate_limit(self, api_name, wait_seconds) -> str:
        return f"<b>⏱️ API限流</b>\n───────────\n接口 <b>{api_name}</b>\n等待 <b>{wait_seconds}</b>秒后重试\n时间 {datetime.now(self.timezone).strftime('%H:%M:%S ET')}\n───────────\n交易暂停，等待限流解除"

    def position_anomaly(self, anomaly_type, details) -> str:
        icons = {"mismatch": "⚠️", "missing": "❌", "cleared": "🔴", "verify_failed": "❗"}
        labels = {"mismatch": "持仓数量不一致", "missing": "持仓丢失", "cleared": "持仓被清空", "verify_failed": "持仓验证失败"}
        return f"<b>{icons.get(anomaly_type, '⚠️')} {labels.get(anomaly_type, '持仓异常')}</b>\n───────────\n{details}\n时间 {datetime.now(self.timezone).strftime('%H:%M:%S ET')}\n───────────\n请检查账户状态"

    def format(self, message: str, msg_type: str = "info", **kwargs: Any) -> str:
        formatters = {
            "entry": self.entry, "exit": self.exit, "partial": self.partial,
            "alert": self.alert, "startup": self.startup, "shutdown": self.shutdown,
            "daily_summary": lambda: self.period_summary("day"),
            "weekly_summary": lambda: self.period_summary("week"),
            "monthly_summary": lambda: self.period_summary("month"),
            "network": self.network, "rate_limit": self.rate_limit,
            "position_anomaly": self.position_anomaly, "system": self.system,
        }
        formatter = formatters.get(msg_type)
        if formatter:
            return formatter(**kwargs)
        lines = message.split("\n")
        first, rest = lines[0] if lines else message, "\n".join(lines[1:])
        return f"<b>{first}</b>\n───────────\n{rest}" if rest else f"<b>{first}</b>"
