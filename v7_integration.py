"""
v7引擎集成层
桥接新信号引擎与现有live_trader.py
"""
import sys
import os
from typing import Optional, Dict
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy.engines import (
    SignalManager, Signal, SignalDirection,
    OpeningRangeEngine, VWAPEngine, BollingerEngine,
    EMAEngine, RSIDivergenceEngine, VIXFilter
)


class V7Integration:
    """
    v7引擎集成层
    
    职责:
    1. 初始化并管理所有引擎
    2. 将K线数据分发到引擎
    3. 将v7 Signal转换为v6.5信号格式
    4. 集成VIX过滤
    """
    
    def __init__(self, cfg: dict):
        self.cfg = cfg
        
        # 初始化信号管理器
        self.signal_manager = SignalManager(cfg)
        
        # 注册5个引擎
        self.signal_manager.register(OpeningRangeEngine(cfg))
        self.signal_manager.register(VWAPEngine(cfg))
        self.signal_manager.register(BollingerEngine(cfg))
        self.signal_manager.register(EMAEngine(cfg))
        self.signal_manager.register(RSIDivergenceEngine(cfg))
        
        # VIX过滤器
        self.vix_filter = VIXFilter(cfg)
        
        # 状态
        self.last_signal: Optional[Signal] = None
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """
        更新所有引擎
        
        Args:
            bar: K线数据 {'open', 'high', 'low', 'close', 'volume', ...}
            et_minute: 当前ET时间(分钟)
        """
        self.signal_manager.update(bar, et_minute)
        
    def update_vix(self, vix: float) -> None:
        """更新VIX值"""
        self.vix_filter.update(vix)
        
    def check_signal(self) -> Optional[Dict]:
        """
        检查信号并转换为v6.5格式
        
        Returns:
            v6.5格式的信号字典，或None
        """
        # VIX过滤
        if not self.vix_filter.should_trade():
            return None
            
        # 检查所有引擎
        signal = self.signal_manager.check()
        if signal is None:
            return None
            
        self.last_signal = signal
        
        # 转换为v6.5格式
        return self._convert_signal(signal)
        
    def _convert_signal(self, sig: Signal) -> Dict:
        """
        将v7 Signal转换为v6.5信号格式
        
        v6.5格式:
        {
            'dir': 'call'/'put',
            'reason': str,
            'price': float,
            'sl': float,
            'tp': float,
            'sl_pct': float,
            'tp_partial_pct': float,
            'timeout_bars': int,
            'pos_mult': float,
            'regime': str,
            'engine': str,  # v7新增
            'strength': float,  # v7新增
        }
        """
        price = sig.entry_price
        dir_str = sig.direction.value
        
        # 从配置获取默认参数
        sl_pct = self.cfg.get('sl', 0.25)
        tp_pct = self.cfg.get('tp', 0.30)
        tp_partial = self.cfg.get('tp_partial_pct', 1.0)
        timeout_bars = self.cfg.get('timeout_stage1_bars', 5)
        
        # 根据引擎调整参数
        pos_mult = 1.0
        regime = 'neutral'
        
        # VIX调整
        vix_mult = self.vix_filter.get_position_mult()
        pos_mult *= vix_mult
        
        # 引擎特定调整
        if sig.engine == 'opening_range':
            # 开盘区间突破：较高置信度
            pos_mult *= 1.1
            regime = 'trending'
        elif sig.engine == 'vwap':
            # VWAP突破：标准参数
            regime = 'trending'
        elif sig.engine == 'bollinger':
            # 布林带挤压：波动率高，收紧止损
            sl_pct *= 0.9
            regime = 'trending'
        elif sig.engine == 'ema':
            # EMA交叉：趋势跟踪
            regime = 'trending'
        elif sig.engine == 'rsi_divergence':
            # RSI背离：逆势信号，降低仓位
            pos_mult *= 0.8
            regime = 'neutral'
            
        # 计算止损止盈价格
        if dir_str == 'call':
            sl_price = price * (1 - sl_pct)
            tp_price = price * (1 + tp_pct)
        else:
            sl_price = price * (1 + sl_pct)
            tp_price = price * (1 - tp_pct)
            
        return {
            'dir': dir_str,
            'reason': f'{sig.engine}: {sig.reason}',
            'price': price,
            'sl': sl_price,
            'tp': tp_price,
            'sl_pct': sl_pct,
            'tp_partial_pct': tp_partial,
            'timeout_bars': timeout_bars,
            'pos_mult': pos_mult,
            'regime': regime,
            # v7新增字段
            'engine': sig.engine,
            'strength': sig.strength,
            'metadata': sig.metadata,
        }
        
    def get_engine_states(self) -> list:
        """获取所有引擎状态(用于dashboard)"""
        return self.signal_manager.get_engine_states()
        
    def get_vix_state(self) -> Dict:
        """获取VIX状态"""
        return self.vix_filter.get_state()
        
    def reset(self) -> None:
        """重置所有引擎(新交易日)"""
        self.signal_manager.reset()
        self.vix_filter.reset()
        self.last_signal = None
