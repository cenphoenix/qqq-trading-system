"""
天才浚浚交易所 - 配置管理模块
管理 settings.json 的读写，提供默认值和验证
"""
import os
import sys
import json
import shutil
from datetime import datetime
from pathlib import Path

# 获取exe所在目录（打包后）或脚本所在目录（开发时）
def get_base_dir():
    """获取应用根目录"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent

BASE_DIR = get_base_dir()
SETTINGS_FILE = BASE_DIR / "settings.json"
ENV_FILE = BASE_DIR / ".env"
BACKUP_DIR = BASE_DIR / "config_backups"

# ===== 默认配置（所有可调参数的默认值）=====
DEFAULT_CONFIG = {
    "_version": "1.0",
    "_description": "天才浚浚交易所 - 交易参数配置",
    "_last_modified": "",

    # ---- 信号参数 ----
    "signal": {
        "symbol": "QQQ.US",
        "rsi_period": 14,
        "rsi_overbought": 75,
        "rsi_oversold": 25,
        "lookback": 3,              # Classic突破窗口
        "lookback_accel": 2,        # Accelerated突破窗口
        "vol_mult": 0.8,            # 成交量倍数阈值
        "min_body": 0.0003,         # 最小K线实体比例
        "max_gap": 0.002,           # 最大跳空 0.20%
        "pullback_confirm": False,   # 是否需要回踩确认
        # 衰竭反转
        "reversal_drop": 0.002,     # 高点跌幅阈值
        "reversal_bounce": 0.001,   # 反弹实体阈值
    },

    # ---- 资金风控 ----
    "risk": {
        "capital": 100000,          # 账户总资金
        "order_pct": 20,            # 单笔下单占总资金百分比
        "sl": 0.25,                 # 止损 25%
        "tp": 0.30,                 # 止盈 30%（旧逻辑兼容）
        "daily_limit": 25,          # 日亏损熔断百分比
        "max_trades": 999,          # 日最大交易次数
        "contract_multiplier": 100, # 每张期权对应股数
        "option_offset": 2.0,       # 期权行权价偏移($2)
        # 动态止盈
        "tp_partial_pct": 1.00,     # 盈利100%平仓一半
        "tp_trail_drop": 0.30,      # 峰值回撤30%全部平仓
        # 跟踪止损
        "stock_trail_pct": 0.003,   # 正股从高点回撤0.3%
        "trail_activate": 0.10,     # 跟踪止损激活 10%
        "trail_drop": 0.05,         # 跟踪止损回撤 5%
        # 正股盈亏风控（按回测 v6.3 口径，优先于期权报价噪声）
        "stock_exit_enabled": True,
        "stock_sl_pct": 0.0025,     # 正股反向0.25%止损
        "stock_tp_pct": 0.0040,     # 正股顺向0.40%止盈
        "stock_trail_activate": 0.0030, # 正股顺向0.30%后启用跟踪
        "stock_trail_drop": 0.0015,     # 从正股峰值回撤0.15%退出
        "put_time_stop_bars": 5,
        "enable_put_entries": True,
        "put_quality_filter": True,
        "put_allowed_signals": [
            "VWAP_Breakout",
            "Kline_Pattern",
            "Granville_Pullback",
        ],
        "put_order_pct": 8.0,
        "price_action_filter": True,
        "price_action_require_put_trend": False,
        "price_action_min_close_location": 0.65,
        "price_action_min_body_ratio": 1.0,
        "price_action_min_direction_bars": 3,
        "price_action_tight_overlap": 0.62,
        "price_action_tight_alternation": 0.43,
        "price_action_require_call_quality": True,
        "price_action_vwap_call_max_range_position": 0.40,
        "price_action_require_ema_call_strong_bar": True,
        "price_action_call_min_close_location": 0.55,
        "price_action_call_min_body_ratio": 0.80,
        "price_action_trend_extend_timeout_bars": 20,
        "brooks_priority_mode": True,
        "brooks_range_call_max_position": 0.40,
        "brooks_range_put_min_position": 0.60,
        "brooks_trend_skip_fixed_stock_tp": True,
        "shadow_signal_tracking": True,
        "shadow_signal_cooldown_bars": 5,
        "shadow_signal_max_per_day": 100,
        "shadow_signal_live_orders": True,
        "shadow_live_order_pos_mult": 0.80,
        "shadow_live_open_pos_mult": 0.50,
        "shadow_live_rejected_pos_mult": 0.60,
        "shadow_live_reduced_rejection_keywords": [
            "质量过滤未通过",
            "Brooks方向冲突",
            "Kline OR下破幅度不足",
            "Granville动量过滤",
        ],
        "shadow_live_afternoon_allowed_signals": [
            "VWAP_Breakout",
        ],
        "shadow_live_sl_pct": 0.26,
        "shadow_live_disable_open_stop_widen": True,
        "trend_day_filter_enabled": True,
        "trend_day_min_bars": 30,
        "trend_day_lookback_bars": 20,
        "trend_day_min_move_pct": 0.0018,
        "trend_day_min_vwap_dist": 0.0010,
        "trend_day_min_sma20_slope": 0.00015,
        "trend_day_countertrend_hard_block": False,
        "market_regime_enabled": True,
        "market_regime_soft_countertrend": True,
        "market_regime_hard_countertrend": False,
        "market_regime_countertrend_pos_mult": 0.25,
        "market_regime_countertrend_sl_pct": 0.24,
        "market_regime_range_breakout_pos_mult": 0.35,
        "market_regime_range_breakout_sl_pct": 0.25,
        "enable_momentum_death_entries": False,
        "momentum_death_pos_mult": 0.65,
        "momentum_death_sl_pct": 0.25,
        "momentum_death_tp_partial_pct": 0.20,
        "momentum_death_timeout_bars": 8,
        "momentum_death_relaxed_put_quality": True,
        "momentum_death_min_rsi_prev": 50,
        "momentum_death_min_rsi_drop": 2.5,
        "momentum_death_rsi_cross": 50,
        "momentum_death_min_macd_prev": 0.03,
        "momentum_death_min_macd_decay": 0.35,
        "momentum_death_min_price_pos": 0.45,
        "disabled_entry_signals": [
            "EMA_Cross",
            "RSI_Reversal",
            "RSI_Overbought",
            "Chan_First_Buy",
            "Momentum_Death",
        ],
        "enable_countertrend_reversal_entries": False,
        "enable_kline_entries": True,
        "kline_quality_filter": True,
        "kline_call_live_patterns": [
            "ORB突破",
            "BB挤压突破",
        ],
        "kline_max_price_pos": 0.82,
        "kline_put_or_break_min_buffer_pct": 0.0020,
        "kline_min_macd_hist": 0.0,
        "kline_min_sma20_slope": 0.0,
        "enable_granville_entries": True,
        "granville_quality_filter": True,
        "granville_max_price_pos": 0.85,
        "granville_min_macd_hist": 0.0,
        "granville_put_min_macd_hist_abs": 0.05,
        "granville_min_sma20_slope": 0.00005,
        "granville_min_vwap_dist": 0.0005,
        "granville_min_dist_pct": 0.20,
        "granville_require_day_direction": True,
        "fast_fail_bars": 7,
        "vwap_fast_option_stop_pct": 22,
        "kline_fast_option_stop_pct": 16,
        "quick_trail_activate_pct": 15,
        "quick_trail_drop_pct": 8,
        "trend_quick_trail_activate_pct": 20,
        "trend_quick_trail_drop_pct": 12,
        "profit_take_tiers": [
            {"profit_pct": 30, "close_pct": 0.30},
            {"profit_pct": 60, "close_pct": 0.30},
            {"profit_pct": 100, "close_remaining": True},
        ],
        "profit_peak_pullback_pct": 30,
        "trend_timeout_bonus_bars": 4,
        "profit_floor_activate_pct": 20,
        "profit_floor_pct": 8,
        "afternoon_put_start_min": 810,
        "afternoon_put_min_vwap_dist": 0.006,
        "afternoon_put_min_sma20_slope_abs": 0.00005,
        # 超时退出
        "timeout_stage1_bars": 10,
        "timeout_stage1_min": 0.30,
        "timeout_stage2_bars": 10,
        "timeout_stage2_min": 0.60,
        "timeout_stage3_bars": 20,
    },

    # ---- 交易窗口 ----
    "trading": {
        "start_time": "09:35",      # 允许入场开始（美东）
        "end_time": "15:50",        # 允许入场结束（美东）
        "check_interval": 20,       # 检测间隔（秒）
        "post_open_cooldown": 15,   # 开盘冷却（分钟）
        "loss_cooldown": 3,         # 连续亏损后冷却次数
    },

    # ---- 飞书通知 ----
    "feishu": {
        "enabled": True,
        "open_id": "YOUR_FEISHU_OPEN_ID",
    },

    # ---- Telegram通知 ----
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
    },
}

# 参数类型映射（用于GUI和验证）
PARAM_TYPES = {
    # signal
    "signal.rsi_period": {"type": "int", "min": 5, "max": 50, "label": "RSI周期"},
    "signal.rsi_overbought": {"type": "int", "min": 60, "max": 90, "label": "RSI超买"},
    "signal.rsi_oversold": {"type": "int", "min": 10, "max": 40, "label": "RSI超卖"},
    "signal.lookback": {"type": "int", "min": 2, "max": 10, "label": "Classic突破窗口"},
    "signal.lookback_accel": {"type": "int", "min": 1, "max": 5, "label": "加速突破窗口"},
    "signal.vol_mult": {"type": "float", "min": 0.3, "max": 3.0, "label": "成交量倍数"},
    "signal.min_body": {"type": "float", "min": 0.0001, "max": 0.01, "label": "最小实体比例"},
    "signal.max_gap": {"type": "float", "min": 0.001, "max": 0.02, "label": "最大跳空"},
    "signal.pullback_confirm": {"type": "bool", "label": "需要回踩确认"},
    "signal.reversal_drop": {"type": "float", "min": 0.001, "max": 0.01, "label": "反转跌幅阈值"},
    "signal.reversal_bounce": {"type": "float", "min": 0.0005, "max": 0.005, "label": "反转反弹阈值"},

    # risk
    "risk.capital": {"type": "float", "min": 1000, "max": 10000000, "label": "账户资金($)"},
    "risk.order_pct": {"type": "float", "min": 1, "max": 50, "label": "单笔仓位(%)"},
    "risk.sl": {"type": "float", "min": 0.05, "max": 0.50, "label": "止损(%)", "display_pct": True},
    "risk.tp": {"type": "float", "min": 0.10, "max": 1.00, "label": "止盈(%)", "display_pct": True},
    "risk.daily_limit": {"type": "float", "min": 5, "max": 50, "label": "日亏损熔断(%)"},
    "risk.max_trades": {"type": "int", "min": 1, "max": 999, "label": "日最大交易次数"},
    "risk.contract_multiplier": {"type": "int", "min": 1, "max": 1000, "label": "合约乘数"},
    "risk.option_offset": {"type": "float", "min": 0.5, "max": 10.0, "label": "行权价偏移($)"},
    "risk.tp_partial_pct": {"type": "float", "min": 0.20, "max": 5.00, "label": "部分止盈(%)", "display_pct": True},
    "risk.tp_trail_drop": {"type": "float", "min": 0.05, "max": 0.50, "label": "峰值回撤平仓(%)", "display_pct": True},
    "risk.stock_trail_pct": {"type": "float", "min": 0.001, "max": 0.02, "label": "正股跟踪止损(%)", "display_pct": True},
    "risk.trail_activate": {"type": "float", "min": 0.05, "max": 0.30, "label": "跟踪止损激活(%)", "display_pct": True},
    "risk.trail_drop": {"type": "float", "min": 0.01, "max": 0.15, "label": "跟踪止损回撤(%)", "display_pct": True},
    "risk.stock_exit_enabled": {"type": "bool", "label": "正股风控"},
    "risk.stock_sl_pct": {"type": "float", "min": 0.001, "max": 0.02, "label": "正股止损(%)", "display_pct": True},
    "risk.stock_tp_pct": {"type": "float", "min": 0.001, "max": 0.03, "label": "正股止盈(%)", "display_pct": True},
    "risk.stock_trail_activate": {"type": "float", "min": 0.001, "max": 0.03, "label": "正股跟踪激活(%)", "display_pct": True},
    "risk.stock_trail_drop": {"type": "float", "min": 0.0005, "max": 0.02, "label": "正股跟踪回撤(%)", "display_pct": True},
    "risk.put_time_stop_bars": {"type": "int", "min": 0, "max": 30, "label": "PUT时间止损"},
    "risk.timeout_stage1_bars": {"type": "int", "min": 2, "max": 15, "label": "超时1(分钟)"},
    "risk.timeout_stage1_min": {"type": "float", "min": 0.05, "max": 0.50, "label": "超时1目标(%)", "display_pct": True},
    "risk.timeout_stage2_bars": {"type": "int", "min": 5, "max": 30, "label": "超时2(分钟)"},
    "risk.timeout_stage2_min": {"type": "float", "min": 0.10, "max": 1.00, "label": "超时2目标(%)", "display_pct": True},
    "risk.timeout_stage3_bars": {"type": "int", "min": 10, "max": 60, "label": "超时3硬退出(分钟)"},

    # trading
    "trading.start_time": {"type": "time", "label": "开盘时间(ET)"},
    "trading.end_time": {"type": "time", "label": "收盘时间(ET)"},
    "trading.check_interval": {"type": "int", "min": 5, "max": 60, "label": "检测间隔(秒)"},
    "trading.post_open_cooldown": {"type": "int", "min": 0, "max": 60, "label": "开盘冷却(分钟)"},
    "trading.loss_cooldown": {"type": "int", "min": 1, "max": 20, "label": "亏损冷却次数"},

    # feishu
    "feishu.enabled": {"type": "bool", "label": "启用飞书推送"},
    "feishu.open_id": {"type": "str", "label": "飞书Open ID"},

    # telegram
    "telegram.enabled": {"type": "bool", "label": "启用Telegram推送"},
    "telegram.bot_token": {"type": "str", "label": "Bot Token"},
    "telegram.chat_id": {"type": "str", "label": "Chat ID"},
}


class ConfigManager:
    """配置管理器 - 单例模式"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._config = {}
        self._observers = []  # 配置变更回调
        self.load()

    def load(self):
        """从 settings.json 加载配置"""
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    self._config = json.load(f)
                # 合并默认值（新字段自动补全）
                self._merge_defaults(self._config, DEFAULT_CONFIG)
            except (json.JSONDecodeError, Exception) as e:
                print(f"[Config] settings.json 读取失败: {e}, 使用默认配置")
                self._config = self._copy_default()
        else:
            self._config = self._copy_default()
            self.save()

    def _merge_defaults(self, loaded, defaults):
        """递归合并默认值，保留用户已修改的"""
        for key, val in defaults.items():
            if key.startswith('_'):
                loaded[key] = val
                continue
            if key not in loaded:
                loaded[key] = val
            elif isinstance(val, dict) and isinstance(loaded[key], dict):
                self._merge_defaults(loaded[key], val)

    def _copy_default(self):
        """深拷贝默认配置"""
        return json.loads(json.dumps(DEFAULT_CONFIG))

    def save(self):
        """保存配置到 settings.json"""
        self._config['_last_modified'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入
        tmp = SETTINGS_FILE.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._config, f, indent=2, ensure_ascii=False)
        tmp.replace(SETTINGS_FILE)

    def backup(self):
        """备份当前配置"""
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        dst = BACKUP_DIR / f"settings_{ts}.json"
        shutil.copy2(SETTINGS_FILE, dst)
        return str(dst)

    def get(self, group, key, default=None):
        """获取配置值: config.get('signal', 'rsi_period')"""
        g = self._config.get(group, {})
        return g.get(key, default)

    def get_all(self, group=None):
        """获取整组或全部配置"""
        if group:
            return self._config.get(group, {})
        return self._config

    def set(self, group, key, value):
        """设置配置值"""
        if group not in self._config:
            self._config[group] = {}
        self._config[group][key] = value

    def set_group(self, group, values: dict):
        """批量设置整组配置"""
        self._config[group] = values

    def get_flat(self):
        """获取扁平化的 CONFIG dict（兼容 live_trader.py 的 CONFIG 格式）"""
        flat = {}
        for group in ['signal', 'risk', 'trading']:
            group_data = self._config.get(group, {})
            for k, v in group_data.items():
                flat[k] = v
        # feishu/telegram 保留嵌套结构，供 _notify() 使用
        flat['feishu'] = self._config.get('feishu', {})
        flat['telegram'] = self._config.get('telegram', {})
        return flat

    def reset_to_default(self, group=None):
        """重置为默认值"""
        if group:
            self._config[group] = json.loads(json.dumps(DEFAULT_CONFIG.get(group, {})))
        else:
            self._config = self._copy_default()
        self.save()

    def notify_change(self):
        """通知所有观察者配置已变更"""
        for cb in self._observers:
            try:
                cb(self._config)
            except Exception as e:
                print(f"[Config] 观察者回调异常: {e}")

    def add_observer(self, callback):
        """添加配置变更观察者"""
        self._observers.append(callback)

    # ===== .env 密钥管理 =====
    @staticmethod
    def load_env():
        """加载 .env 文件"""
        env = {}
        if ENV_FILE.exists():
            with open(ENV_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        env[k.strip()] = v.strip()
        return env

    @staticmethod
    def save_env(env_dict: dict):
        """保存 .env 文件"""
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for k, v in env_dict.items():
            lines.append(f"{k}={v}")
        tmp = ENV_FILE.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        tmp.replace(ENV_FILE)

    @staticmethod
    def has_env_keys():
        """检查 .env 是否已配置认证（OAuth2: client_id / 旧版: app_key）"""
        env = ConfigManager.load_env()
        # OAuth2 (当前)
        if env.get('LONGBRIDGE_CLIENT_ID'):
            return True
        # 旧版 API Key (兼容)
        return bool(env.get('LONGPORT_APP_KEY') and env.get('LONGPORT_ACCESS_TOKEN'))


# ===== 便捷函数 =====
def get_config():
    """获取配置管理器实例"""
    return ConfigManager()


def get_flat_config():
    """获取扁平配置（兼容 CONFIG 格式）"""
    return ConfigManager().get_flat()


# 向后兼容：导出默认配置供 live_trader.py 使用
def get_default_config() -> dict:
    """获取默认配置字典（兼容旧代码）"""
    flat = get_flat_config()
    # 补充旧代码需要的额外字段
    flat['lookback_accel'] = flat.get('lookback_accel', 2)
    return flat
