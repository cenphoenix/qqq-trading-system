"""
RSI背离引擎 (RSI Divergence)
价格创新高/新低但RSI未同步，预示趋势反转
"""
from typing import Optional, Dict, List
from .base import BaseEngine, Signal, SignalDirection


class RSIDivergenceEngine(BaseEngine):
    """
    RSI背离引擎
    
    逻辑:
    1. 检测RSI超买(>70)/超卖(<30)区域
    2. 价格创新高但RSI未创新高 → 顶背离(PUT)
    3. 价格创新低但RSI未创新低 → 底背离(CALL)
    4. 多周期验证(1分钟+5分钟)
    
    强度计算:
    - RSI极端程度(越远离50越强)
    - 背离幅度越大，强度越高
    """
    
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "rsi_divergence"
        self.priority = 5  # 最低优先级(逆势风险高)
        
        # 参数
        self.rsi_period = cfg.get('rsi_period', 14)
        self.rsi_ob = cfg.get('rsi_overbought', 70)  # 超买阈值
        self.rsi_os = cfg.get('rsi_oversold', 30)     # 超卖阈值
        self.lookback = cfg.get('rsi_div_lookback', 20)  # 背离检测回溯
        
        # RSI状态
        self.rsi = 50.0
        self.rsi_history = []
        
        # 背离检测
        self._price_highs = []  # 价格高点
        self._price_lows = []   # 价格低点
        self._rsi_highs = []    # RSI高点
        self._rsi_lows = []     # RSI低点
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """更新数据并计算RSI"""
        super().update(bar, et_minute)
        
        # 计算RSI
        self.rsi = self._calc_rsi()
        self.rsi_history.append(self.rsi)
        
        if len(self.rsi_history) > 200:
            self.rsi_history = self.rsi_history[-200:]
            
        # 记录极值点
        self._track_extremes()
        
    def _calc_rsi(self) -> float:
        """计算RSI"""
        period = self.rsi_period
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
        
    def _track_extremes(self) -> None:
        """跟踪价格和RSI的极值点"""
        if len(self.closes) < 5:
            return
            
        # 检测局部高点(比前后2根K线都高)
        if len(self.closes) >= 5:
            c = self.closes[-3]
            if c > self.closes[-5] and c > self.closes[-4] and c > self.closes[-2] and c > self.closes[-1]:
                self._price_highs.append((len(self.closes) - 3, c))
                self._rsi_highs.append((len(self.rsi_history) - 3, self.rsi_history[-3]))
                
        # 检测局部低点
        if len(self.closes) >= 5:
            c = self.closes[-3]
            if c < self.closes[-5] and c < self.closes[-4] and c < self.closes[-2] and c < self.closes[-1]:
                self._price_lows.append((len(self.closes) - 3, c))
                self._rsi_lows.append((len(self.rsi_history) - 3, self.rsi_history[-3]))
                
        # 限制长度
        max_points = 20
        if len(self._price_highs) > max_points:
            self._price_highs = self._price_highs[-max_points:]
            self._rsi_highs = self._rsi_highs[-max_points:]
        if len(self._price_lows) > max_points:
            self._price_lows = self._price_lows[-max_points:]
            self._rsi_lows = self._rsi_lows[-max_points:]
            
    def check(self) -> Optional[Signal]:
        """检查RSI背离"""
        if not self._initialized or len(self.rsi_history) < self.lookback:
            return None
            
        price = self.closes[-1]
        
        # 检查底背离 (CALL): 价格创新低但RSI未创新低
        if self.rsi < self.rsi_os:
            if len(self._price_lows) >= 2 and len(self._rsi_lows) >= 2:
                # 最近两个低点
                prev_price_low = self._price_lows[-2][1]
                curr_price_low = self._price_lows[-1][1]
                prev_rsi_low = self._rsi_lows[-2][1]
                curr_rsi_low = self._rsi_lows[-1][1]
                
                # 价格创新低，RSI未创新低
                if curr_price_low < prev_price_low and curr_rsi_low > prev_rsi_low:
                    rsi_extremity = abs(self.rsi - 50)
                    divergence = (curr_rsi_low - prev_rsi_low) - (curr_price_low - prev_price_low) / prev_price_low * 100
                    strength = min(100, 50 + rsi_extremity + divergence * 10)
                    
                    return Signal(
                        engine=self.name,
                        direction=SignalDirection.CALL,
                        strength=strength,
                        entry_price=price,
                        reason=f"RSI底背离 RSI={self.rsi:.1f}<{self.rsi_os}",
                        metadata={
                            'rsi': self.rsi,
                            'prev_price_low': prev_price_low,
                            'curr_price_low': curr_price_low,
                            'prev_rsi_low': prev_rsi_low,
                            'curr_rsi_low': curr_rsi_low,
                        }
                    )
                    
        # 检查顶背离 (PUT): 价格创新高但RSI未创新高
        if self.rsi > self.rsi_ob:
            if len(self._price_highs) >= 2 and len(self._rsi_highs) >= 2:
                prev_price_high = self._price_highs[-2][1]
                curr_price_high = self._price_highs[-1][1]
                prev_rsi_high = self._rsi_highs[-2][1]
                curr_rsi_high = self._rsi_highs[-1][1]
                
                # 价格创新高，RSI未创新高
                if curr_price_high > prev_price_high and curr_rsi_high < prev_rsi_high:
                    rsi_extremity = abs(self.rsi - 50)
                    divergence = (prev_rsi_high - curr_rsi_high) - (curr_price_high - prev_price_high) / prev_price_high * 100
                    strength = min(100, 50 + rsi_extremity + divergence * 10)
                    
                    return Signal(
                        engine=self.name,
                        direction=SignalDirection.PUT,
                        strength=strength,
                        entry_price=price,
                        reason=f"RSI顶背离 RSI={self.rsi:.1f}>{self.rsi_ob}",
                        metadata={
                            'rsi': self.rsi,
                            'prev_price_high': prev_price_high,
                            'curr_price_high': curr_price_high,
                            'prev_rsi_high': prev_rsi_high,
                            'curr_rsi_high': curr_rsi_high,
                        }
                    )
                    
        return None
        
    def get_state(self) -> Dict:
        """获取引擎状态"""
        state = super().get_state()
        state.update({
            'rsi': self.rsi,
            'rsi_ob': self.rsi_ob,
            'rsi_os': self.rsi_os,
            'price_highs_count': len(self._price_highs),
            'price_lows_count': len(self._price_lows),
        })
        return state
        
    def reset(self) -> None:
        """重置"""
        super().reset()
        self.rsi = 50.0
        self.rsi_history.clear()
        self._price_highs.clear()
        self._price_lows.clear()
        self._rsi_highs.clear()
        self._rsi_lows.clear()
