"""
信号引擎基类 - v7多引擎架构
所有引擎继承此类，统一接口
"""
from dataclasses import dataclass
from typing import Optional, Dict
from enum import Enum


class SignalDirection(Enum):
    CALL = "call"
    PUT = "put"
    NEUTRAL = "neutral"


@dataclass
class Signal:
    """标准化信号输出"""
    engine: str                    # 引擎名称
    direction: SignalDirection     # call/put
    strength: float                # 信号强度 0-100
    entry_price: float             # 建议入场价
    reason: str                    # 信号原因
    metadata: Optional[Dict] = None # 引擎特定数据
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class BaseEngine:
    """
    信号引擎基类
    
    所有引擎必须实现:
    - update(bar, et_minute): 更新数据
    - check() -> Optional[Signal]: 检查信号
    - name: 引擎名称
    - priority: 基础优先级 (1-5)
    """
    
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = "base"
        self.priority = 3  # 默认优先级
        self.enabled = True
        
        # 历史数据缓存
        self.closes = []
        self.highs = []
        self.lows = []
        self.volumes = []
        self.bars = []
        
        # 会话数据
        self.session_high = 0
        self.session_low = 999999
        self._initialized = False
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """
        每根K线更新数据
        
        Args:
            bar: {'open', 'high', 'low', 'close', 'volume', 'timestamp'}
            et_minute: 当前ET时间(分钟), 570=09:30, 960=16:00
        """
        self.bars.append(bar)
        self.closes.append(bar['close'])
        self.highs.append(bar['high'])
        self.lows.append(bar['low'])
        self.volumes.append(bar['volume'])
        
        self.session_high = max(self.session_high, bar['high'])
        self.session_low = min(self.session_low, bar['low'])
        
        # 限制历史长度
        max_len = 500
        if len(self.closes) > max_len:
            self.closes = self.closes[-max_len:]
            self.highs = self.highs[-max_len:]
            self.lows = self.lows[-max_len:]
            self.volumes = self.volumes[-max_len:]
            self.bars = self.bars[-max_len:]
            
        self._initialized = True
        
    def check(self) -> Optional[Signal]:
        """
        检查是否有信号
        
        Returns:
            Signal对象或None
        """
        raise NotImplementedError
        
    def get_state(self) -> Dict:
        """获取引擎状态(用于dashboard显示)"""
        return {
            'name': self.name,
            'enabled': self.enabled,
            'priority': self.priority,
            'bars_count': len(self.closes),
        }
        
    def reset(self) -> None:
        """重置引擎状态(新交易日)"""
        self.closes.clear()
        self.highs.clear()
        self.lows.clear()
        self.volumes.clear()
        self.bars.clear()
        self.session_high = 0
        self.session_low = 999999
        self._initialized = False
