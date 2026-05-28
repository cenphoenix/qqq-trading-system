"""
VWAP突破引擎
成交量加权平均价是机构交易者最常用的参考指标
"""
from typing import Optional, Dict
from .base import BaseEngine, Signal, SignalDirection


class VWAPEngine(BaseEngine):
    """
    VWAP突破引擎
    
    逻辑:
    1. 计算实时VWAP
    2. 价格突破VWAP + 成交量放大 → 信号
    3. 突破幅度需超过动态阈值，避免假突破
    
    强度计算:
    - 价格离VWAP越远，强度越高
    - 成交量放大倍数越高，强度越高
    """
    
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "vwap"
        self.priority = 2
        
        # 参数
        self.vol_mult = cfg.get('vwap_vol_mult', 1.3)  # 成交量放大倍数
        self.breakout_pct = cfg.get('vwap_breakout_pct', 0.001)  # 突破阈值(0.1%)
        
        # VWAP计算
        self.vwap_cum_tp_vol = 0.0  # 累计(typical_price * volume)
        self.vwap_cum_vol = 0       # 累计成交量
        self.vwap = 0.0
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """更新数据并计算VWAP"""
        super().update(bar, et_minute)
        
        # 计算VWAP
        typical_price = (bar['high'] + bar['low'] + bar['close']) / 3.0
        self.vwap_cum_tp_vol += typical_price * bar['volume']
        self.vwap_cum_vol += bar['volume']
        self.vwap = self.vwap_cum_tp_vol / self.vwap_cum_vol if self.vwap_cum_vol > 0 else bar['close']
        
    def check(self) -> Optional[Signal]:
        """检查VWAP突破"""
        if not self._initialized or len(self.closes) < 20:
            return None
            
        price = self.closes[-1]
        bar = self.bars[-1]
        
        # 成交量均值
        vol_avg = sum(self.volumes[-20:]) / 20
        if vol_avg <= 0:
            return None
            
        vol_ratio = bar['volume'] / vol_avg
        
        # 突破阈值
        threshold = max(self.vwap * self.breakout_pct, 0.05)  # 最小$0.05
        
        # 上方突破 (CALL)
        if price > self.vwap + threshold and vol_ratio >= self.vol_mult:
            breakout_pct = (price - self.vwap) / self.vwap * 100
            strength = min(100, 60 + breakout_pct * 300 + (vol_ratio - 1) * 15)
            
            return Signal(
                engine=self.name,
                direction=SignalDirection.CALL,
                strength=strength,
                entry_price=price,
                reason=f"VWAP突破 ${self.vwap:.2f} +{breakout_pct:.3f}%",
                metadata={
                    'vwap': self.vwap,
                    'vol_ratio': vol_ratio,
                    'breakout_pct': breakout_pct,
                }
            )
            
        # 下方突破 (PUT)
        if price < self.vwap - threshold and vol_ratio >= self.vol_mult:
            breakout_pct = (self.vwap - price) / self.vwap * 100
            strength = min(100, 60 + breakout_pct * 300 + (vol_ratio - 1) * 15)
            
            return Signal(
                engine=self.name,
                direction=SignalDirection.PUT,
                strength=strength,
                entry_price=price,
                reason=f"VWAP跌破 ${self.vwap:.2f} -{breakout_pct:.3f}%",
                metadata={
                    'vwap': self.vwap,
                    'vol_ratio': vol_ratio,
                    'breakout_pct': breakout_pct,
                }
            )
            
        return None
        
    def get_state(self) -> Dict:
        """获取引擎状态"""
        state = super().get_state()
        state.update({
            'vwap': self.vwap,
            'vwap_cum_vol': self.vwap_cum_vol,
        })
        return state
        
    def reset(self) -> None:
        """重置"""
        super().reset()
        self.vwap_cum_tp_vol = 0.0
        self.vwap_cum_vol = 0
        self.vwap = 0.0
