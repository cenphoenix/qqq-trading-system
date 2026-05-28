"""
布林带挤压引擎 (Bollinger Squeeze)
波动率收缩→扩张，积蓄能量后爆发
"""
from typing import Optional, Dict
import numpy as np
from .base import BaseEngine, Signal, SignalDirection


class BollingerEngine(BaseEngine):
    """
    布林带挤压引擎
    
    逻辑:
    1. 计算布林带(20,2)
    2. 带宽降至近20周期低位 → 挤压状态
    3. 价格突破上/下轨 + RSI/MACD确认 → 信号
    
    强度计算:
    - 挤压时间越长，强度越高
    - 突破幅度越大，强度越高
    """
    
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "bollinger"
        self.priority = 3
        
        # 参数
        self.bb_period = cfg.get('bb_period', 20)
        self.bb_std = cfg.get('bb_std', 2.0)
        self.squeeze_threshold = cfg.get('bb_squeeze_threshold', 0.2)  # 带宽百分位阈值
        self.squeeze_lookback = cfg.get('bb_squeeze_lookback', 20)  # 挤压检测回溯
        
        # 状态
        self.bb_upper = 0.0
        self.bb_lower = 0.0
        self.bb_mid = 0.0
        self.bb_width = 0.0
        self.bb_width_history = []
        self.squeeze_count = 0  # 持续挤压的K线数
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """更新数据并计算布林带"""
        super().update(bar, et_minute)
        
        if len(self.closes) < self.bb_period:
            return
            
        # 计算布林带
        closes = np.array(self.closes[-self.bb_period:])
        self.bb_mid = np.mean(closes)
        std = np.std(closes)
        
        self.bb_upper = self.bb_mid + self.bb_std * std
        self.bb_lower = self.bb_mid - self.bb_std * std
        self.bb_width = (self.bb_upper - self.bb_lower) / self.bb_mid * 100 if self.bb_mid > 0 else 0
        
        # 记录带宽历史
        self.bb_width_history.append(self.bb_width)
        if len(self.bb_width_history) > 100:
            self.bb_width_history = self.bb_width_history[-100:]
            
        # 检测挤压状态
        if len(self.bb_width_history) >= self.squeeze_lookback:
            recent_widths = self.bb_width_history[-self.squeeze_lookback:]
            percentile = sum(1 for w in recent_widths if w < self.bb_width) / len(recent_widths)
            
            if percentile < self.squeeze_threshold:
                self.squeeze_count += 1
            else:
                self.squeeze_count = 0
                
    def check(self) -> Optional[Signal]:
        """检查布林带挤压突破"""
        if not self._initialized or len(self.closes) < self.bb_period:
            return None
            
        # 必须有挤压状态
        if self.squeeze_count < 3:  # 至少3根K线挤压
            return None
            
        price = self.closes[-1]
        bar = self.bars[-1]
        
        # RSI确认
        rsi = self._calc_rsi(14)
        
        # 检查上轨突破 (CALL)
        if price > self.bb_upper:
            if rsi > 50:  # RSI确认多头
                breakout_pct = (price - self.bb_upper) / self.bb_upper * 100
                strength = min(100, 50 + self.squeeze_count * 2 + breakout_pct * 500)
                
                return Signal(
                    engine=self.name,
                    direction=SignalDirection.CALL,
                    strength=strength,
                    entry_price=price,
                    reason=f"BB挤压突破上轨 {self.bb_upper:.2f}, 挤压{self.squeeze_count}根",
                    metadata={
                        'bb_upper': self.bb_upper,
                        'bb_lower': self.bb_lower,
                        'bb_mid': self.bb_mid,
                        'bb_width': self.bb_width,
                        'squeeze_count': self.squeeze_count,
                        'rsi': rsi,
                    }
                )
                
        # 检查下轨突破 (PUT)
        if price < self.bb_lower:
            if rsi < 50:  # RSI确认空头
                breakout_pct = (self.bb_lower - price) / self.bb_lower * 100
                strength = min(100, 50 + self.squeeze_count * 2 + breakout_pct * 500)
                
                return Signal(
                    engine=self.name,
                    direction=SignalDirection.PUT,
                    strength=strength,
                    entry_price=price,
                    reason=f"BB挤压突破下轨 {self.bb_lower:.2f}, 挤压{self.squeeze_count}根",
                    metadata={
                        'bb_upper': self.bb_upper,
                        'bb_lower': self.bb_lower,
                        'bb_mid': self.bb_mid,
                        'bb_width': self.bb_width,
                        'squeeze_count': self.squeeze_count,
                        'rsi': rsi,
                    }
                )
                
        return None
        
    def _calc_rsi(self, period: int = 14) -> float:
        """计算RSI"""
        if len(self.closes) < period + 1:
            return 50.0
            
        changes = [self.closes[i] - self.closes[i-1] for i in range(-period, 0)]
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0
        
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
        
    def get_state(self) -> Dict:
        """获取引擎状态"""
        state = super().get_state()
        state.update({
            'bb_upper': self.bb_upper,
            'bb_lower': self.bb_lower,
            'bb_mid': self.bb_mid,
            'bb_width': self.bb_width,
            'squeeze_count': self.squeeze_count,
        })
        return state
        
    def reset(self) -> None:
        """重置"""
        super().reset()
        self.bb_upper = 0.0
        self.bb_lower = 0.0
        self.bb_mid = 0.0
        self.bb_width = 0.0
        self.bb_width_history.clear()
        self.squeeze_count = 0
