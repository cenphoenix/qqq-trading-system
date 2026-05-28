"""
VIX波动率过滤器
根据VIX指数水平调整仓位
"""
from typing import Dict, Optional
from enum import Enum


class VIXRegime(Enum):
    """VIX波动率区间"""
    LOW = "low"           # VIX < 15: 市场过于平静
    NORMAL = "normal"     # 15-25: 正常波动
    HIGH = "high"         # 25-35: 高波动
    EXTREME = "extreme"   # > 35: 极端恐慌


class VIXFilter:
    """
    VIX波动率过滤器
    
    4档仓位自适应:
    - VIX < 15: 降低仓位或暂停(0DTE theta衰减过快)
    - VIX 15-25: 标准仓位
    - VIX 25-35: 加大仓位，收紧止损
    - VIX > 35: 保守模式，仅交易深度实值期权
    """
    
    def __init__(self, cfg: dict):
        self.cfg = cfg
        
        # VIX阈值
        self.low_threshold = cfg.get('vix_low', 15)
        self.high_threshold = cfg.get('vix_high', 25)
        self.extreme_threshold = cfg.get('vix_extreme', 35)
        
        # 仓位系数
        self.position_multipliers = {
            VIXRegime.LOW: 0.5,      # 降低50%
            VIXRegime.NORMAL: 1.0,    # 标准仓位
            VIXRegime.HIGH: 1.2,      # 增加20%
            VIXRegime.EXTREME: 0.6,   # 保守模式
        }
        
        # 止损系数
        self.sl_multipliers = {
            VIXRegime.LOW: 1.2,       # 放宽止损
            VIXRegime.NORMAL: 1.0,    # 标准止损
            VIXRegime.HIGH: 0.8,      # 收紧止损
            VIXRegime.EXTREME: 0.6,   # 紧止损
        }
        
        # 状态
        self.current_vix = 0.0
        self.current_regime = VIXRegime.NORMAL
        self._vix_history = []
        
    def update(self, vix: float) -> None:
        """更新VIX值"""
        self.current_vix = vix
        self._vix_history.append(vix)
        
        if len(self._vix_history) > 100:
            self._vix_history = self._vix_history[-100:]
            
        # 判断regime
        if vix < self.low_threshold:
            self.current_regime = VIXRegime.LOW
        elif vix < self.high_threshold:
            self.current_regime = VIXRegime.NORMAL
        elif vix < self.extreme_threshold:
            self.current_regime = VIXRegime.HIGH
        else:
            self.current_regime = VIXRegime.EXTREME
            
    def get_position_mult(self) -> float:
        """获取仓位系数"""
        return self.position_multipliers.get(self.current_regime, 1.0)
        
    def get_sl_mult(self) -> float:
        """获取止损系数"""
        return self.sl_multipliers.get(self.current_regime, 1.0)
        
    def should_trade(self) -> bool:
        """是否应该交易"""
        # VIX过低时，0DTE期权theta衰减太快，不建议交易
        if self.current_vix > 0 and self.current_vix < self.low_threshold:
            return False
        return True
        
    def get_state(self) -> Dict:
        """获取状态"""
        return {
            'vix': self.current_vix,
            'regime': self.current_regime.value,
            'position_mult': self.get_position_mult(),
            'sl_mult': self.get_sl_mult(),
            'should_trade': self.should_trade(),
        }
        
    def reset(self) -> None:
        """重置"""
        self.current_vix = 0.0
        self.current_regime = VIXRegime.NORMAL
        self._vix_history.clear()
