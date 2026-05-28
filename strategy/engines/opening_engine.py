"""
开盘区间突破引擎 (Opening Range Breakout)
前15-30分钟形成价格区间，突破方向决定日内趋势
"""
from typing import Optional, Dict
from .base import BaseEngine, Signal, SignalDirection


class OpeningRangeEngine(BaseEngine):
    """
    开盘区间突破引擎
    
    逻辑:
    1. 前15分钟(09:30-09:45)建立高低点区间
    2. 价格突破区间上沿 + 放量 → CALL信号
    3. 价格突破区间下沿 + 放量 → PUT信号
    4. 区间中点作为止损参考
    
    强度计算:
    - 突破幅度越大，强度越高
    - 成交量放大倍数越高，强度越高
    """
    
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "opening_range"
        self.priority = 1  # 最高优先级
        
        # 区间参数
        self.range_minutes = cfg.get('orb_range_minutes', 15)  # 区间建立时间
        self.breakout_mult = cfg.get('orb_breakout_mult', 0.001)  # 突破幅度阈值(0.1%)
        self.vol_mult = cfg.get('orb_vol_mult', 1.5)  # 成交量放大倍数
        
        # 区间状态
        self.range_high = 0
        self.range_low = 999999
        self.range_established = False
        self.range_bars = 0
        self._range_start_minute = 570  # 09:30 ET
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """更新数据，同时建立开盘区间"""
        super().update(bar, et_minute)
        
        # 建立区间阶段
        if not self.range_established:
            if et_minute < self._range_start_minute + self.range_minutes:
                self.range_high = max(self.range_high, bar['high'])
                self.range_low = min(self.range_low, bar['low'])
                self.range_bars += 1
            else:
                self.range_established = True
                
    def check(self) -> Optional[Signal]:
        """检查开盘区间突破"""
        if not self._initialized or not self.range_established:
            return None
            
        # 区间太窄或太宽都不合理
        if self.range_bars < 5:
            return None
            
        price = self.closes[-1]
        bar = self.bars[-1]
        range_size = self.range_high - self.range_low
        
        if range_size <= 0:
            return None
            
        # 计算突破幅度阈值
        threshold = max(range_size * 0.3, price * self.breakout_mult)
        
        # 成交量均值
        vol_avg = sum(self.volumes[-20:]) / min(len(self.volumes), 20) if self.volumes else 0
        
        # 检查上沿突破 (CALL)
        if price > self.range_high + threshold:
            vol_ratio = bar['volume'] / vol_avg if vol_avg > 0 else 1
            if vol_ratio >= self.vol_mult:
                # 计算强度: 突破幅度 + 成交量放大
                breakout_pct = (price - self.range_high) / self.range_high * 100
                strength = min(100, 50 + breakout_pct * 500 + (vol_ratio - 1) * 20)
                
                return Signal(
                    engine=self.name,
                    direction=SignalDirection.CALL,
                    strength=strength,
                    entry_price=price,
                    reason=f"ORB突破上沿 {self.range_high:.2f} +{breakout_pct:.2f}%",
                    metadata={
                        'range_high': self.range_high,
                        'range_low': self.range_low,
                        'range_mid': (self.range_high + self.range_low) / 2,
                        'vol_ratio': vol_ratio,
                    }
                )
                
        # 检查下沿突破 (PUT)
        if price < self.range_low - threshold:
            vol_ratio = bar['volume'] / vol_avg if vol_avg > 0 else 1
            if vol_ratio >= self.vol_mult:
                breakout_pct = (self.range_low - price) / self.range_low * 100
                strength = min(100, 50 + breakout_pct * 500 + (vol_ratio - 1) * 20)
                
                return Signal(
                    engine=self.name,
                    direction=SignalDirection.PUT,
                    strength=strength,
                    entry_price=price,
                    reason=f"ORB突破下沿 {self.range_low:.2f} -{breakout_pct:.2f}%",
                    metadata={
                        'range_high': self.range_high,
                        'range_low': self.range_low,
                        'range_mid': (self.range_high + self.range_low) / 2,
                        'vol_ratio': vol_ratio,
                    }
                )
                
        return None
        
    def get_state(self) -> Dict:
        """获取引擎状态"""
        state = super().get_state()
        state.update({
            'range_high': self.range_high,
            'range_low': self.range_low,
            'range_established': self.range_established,
            'range_bars': self.range_bars,
        })
        return state
        
    def reset(self) -> None:
        """重置"""
        super().reset()
        self.range_high = 0
        self.range_low = 999999
        self.range_established = False
        self.range_bars = 0
