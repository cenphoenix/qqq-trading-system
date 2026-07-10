"""
信号管理器 - v7多引擎信号聚合
收集所有引擎信号，选最强的一个输出
"""
from typing import Optional, Dict, List
from .base import BaseEngine, Signal


class SignalManager:
    """
    多引擎信号聚合器
    
    职责:
    1. 管理所有引擎实例
    2. 分发K线数据到各引擎
    3. 收集信号，选最强的一个
    4. 提供引擎状态给dashboard
    """
    
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.engines: List[BaseEngine] = []
        self.last_signal: Optional[Signal] = None
        self.signal_history: List[Signal] = []
        
    def register(self, engine: BaseEngine) -> None:
        """注册引擎"""
        self.engines.append(engine)
        self.engines.sort(key=lambda e: e.priority)
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """更新所有引擎"""
        for engine in self.engines:
            if engine.enabled:
                engine.update(bar, et_minute)
                
    def check(self) -> Optional[Signal]:
        """
        检查所有引擎，返回最强信号
        
        Returns:
            最强信号或None
        """
        signals: List[Signal] = []
        
        for engine in self.engines:
            if not engine.enabled:
                continue
            try:
                sig = engine.check()
                if sig is not None:
                    signals.append(sig)
            except Exception:
                pass
                
        if not signals:
            return None
            
        # 按强度排序，选最强的
        signals.sort(key=lambda s: s.strength, reverse=True)
        best = signals[0]
        
        self.last_signal = best
        self.signal_history.append(best)
        
        # 限制历史长度
        if len(self.signal_history) > 100:
            self.signal_history = self.signal_history[-100:]
            
        return best
        
    def get_all_signals(self) -> List[Signal]:
        """获取所有引擎的当前信号(用于dashboard)"""
        signals = []
        for engine in self.engines:
            if engine.enabled:
                try:
                    sig = engine.check()
                    if sig:
                        signals.append(sig)
                except Exception:
                    pass
        return signals
        
    def get_engine_states(self) -> List[Dict]:
        """获取所有引擎状态(用于dashboard)"""
        return [e.get_state() for e in self.engines]
        
    def reset(self) -> None:
        """重置所有引擎"""
        for engine in self.engines:
            engine.reset()
        self.last_signal = None
        self.signal_history.clear()
        
    def enable_engine(self, name: str) -> None:
        """启用指定引擎"""
        for e in self.engines:
            if e.name == name:
                e.enabled = True
                break
                
    def disable_engine(self, name: str) -> None:
        """禁用指定引擎"""
        for e in self.engines:
            if e.name == name:
                e.enabled = False
                break
