"""
EMA交叉引擎 (Exponential Moving Average Crossover)
9/21 EMA交叉为主信号，50 EMA作为趋势过滤
"""
from typing import Optional, Dict
from .base import BaseEngine, Signal, SignalDirection


class EMAEngine(BaseEngine):
    """
    EMA交叉引擎
    
    逻辑:
    1. 计算9/21/50 EMA
    2. 9 EMA上穿21 EMA + 价格>50 EMA → CALL
    3. 9 EMA下穿21 EMA + 价格<50 EMA → PUT
    4. ADX过滤横盘震荡
    
    强度计算:
    - EMA斜率越陡，强度越高
    - 与50 EMA距离越远，趋势越强
    """
    
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "ema"
        self.priority = 4
        
        # 参数
        self.ema_fast = cfg.get('ema_fast', 9)
        self.ema_mid = cfg.get('ema_mid', 21)
        self.ema_slow = cfg.get('ema_slow', 50)
        self.adx_threshold = cfg.get('ema_adx_threshold', 20)  # ADX阈值
        
        # EMA状态
        self.ema9 = None
        self.ema21 = None
        self.ema50 = None
        self.ema9_prev = None
        self.ema21_prev = None
        
        # ADX计算
        self._tr_list = []
        self._plus_dm = []
        self._minus_dm = []
        self.adx = 0.0
        
        # ET时间
        self._et_minute = 0
        
    def update(self, bar: dict, et_minute: int = 0) -> None:
        """更新数据并计算EMA"""
        super().update(bar, et_minute)
        self._et_minute = et_minute
        
        price = bar['close']
        
        # EMA计算
        if self.ema9 is None:
            if len(self.closes) >= self.ema_fast:
                self.ema9 = sum(self.closes[-self.ema_fast:]) / self.ema_fast
            else:
                self.ema9 = price
                
        if self.ema21 is None:
            if len(self.closes) >= self.ema_mid:
                self.ema21 = sum(self.closes[-self.ema_mid:]) / self.ema_mid
            else:
                self.ema21 = price
                
        if self.ema50 is None:
            if len(self.closes) >= self.ema_slow:
                self.ema50 = sum(self.closes[-self.ema_slow:]) / self.ema_slow
            else:
                self.ema50 = price
                
        # 保存前值
        self.ema9_prev = self.ema9
        self.ema21_prev = self.ema21
        
        # 更新EMA
        k9 = 2.0 / (self.ema_fast + 1)
        k21 = 2.0 / (self.ema_mid + 1)
        k50 = 2.0 / (self.ema_slow + 1)
        
        self.ema9 = price * k9 + self.ema9 * (1 - k9)
        self.ema21 = price * k21 + self.ema21 * (1 - k21)
        self.ema50 = price * k50 + self.ema50 * (1 - k50)
        
        # ADX计算
        self._update_adx(bar)
        
    def _update_adx(self, bar: dict) -> None:
        """更新ADX"""
        if len(self.bars) < 2:
            return
            
        prev_bar = self.bars[-2]
        
        # True Range
        tr = max(
            bar['high'] - bar['low'],
            abs(bar['high'] - prev_bar['close']),
            abs(bar['low'] - prev_bar['close'])
        )
        self._tr_list.append(tr)
        
        # +DM / -DM
        up_move = bar['high'] - prev_bar['high']
        down_move = prev_bar['low'] - bar['low']
        
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0
        
        self._plus_dm.append(plus_dm)
        self._minus_dm.append(minus_dm)
        
        # 限制长度
        period = 14
        if len(self._tr_list) > period * 2:
            self._tr_list = self._tr_list[-period*2:]
            self._plus_dm = self._plus_dm[-period*2:]
            self._minus_dm = self._minus_dm[-period*2:]
            
        # 计算ADX
        if len(self._tr_list) >= period:
            avg_tr = sum(self._tr_list[-period:]) / period
            avg_plus = sum(self._plus_dm[-period:]) / period
            avg_minus = sum(self._minus_dm[-period:]) / period
            
            if avg_tr > 0:
                plus_di = (avg_plus / avg_tr) * 100
                minus_di = (avg_minus / avg_tr) * 100
                
                di_sum = plus_di + minus_di
                if di_sum > 0:
                    dx = abs(plus_di - minus_di) / di_sum * 100
                    self.adx = dx  # 简化版，实际应该用Wilder平滑
                    
    def check(self) -> Optional[Signal]:
        """检查EMA交叉"""
        if not self._initialized:
            return None
            
        if self.ema9 is None or self.ema21 is None or self.ema50 is None:
            return None
        if self.ema9_prev is None or self.ema21_prev is None:
            return None
            
        # 开盘前10分钟禁止EMA信号（09:35-09:45容易假突破）
        if 575 <= self._et_minute < 585:
            return None
            
        price = self.closes[-1]
        
        # ADX过滤：必须有足够的趋势强度
        if self.adx < self.adx_threshold:
            return None
            
        # 金叉: 9 EMA上穿21 EMA (CALL)
        if self.ema9 > self.ema21 and self.ema9_prev <= self.ema21_prev:
            if price > self.ema50:  # 价格在50 EMA上方
                # 计算强度
                ema_spread = (self.ema9 - self.ema21) / self.ema21 * 100
                trend_dist = (price - self.ema50) / self.ema50 * 100
                strength = min(100, 60 + ema_spread * 1000 + trend_dist * 50 + self.adx)
                
                return Signal(
                    engine=self.name,
                    direction=SignalDirection.CALL,
                    strength=strength,
                    entry_price=price,
                    reason=f"EMA金叉 9>{self.ema21:.2f}, ADX={self.adx:.1f}",
                    metadata={
                        'ema9': self.ema9,
                        'ema21': self.ema21,
                        'ema50': self.ema50,
                        'adx': self.adx,
                    }
                )
                
        # 死叉: 9 EMA下穿21 EMA (PUT)
        if self.ema9 < self.ema21 and self.ema9_prev >= self.ema21_prev:
            if price < self.ema50:  # 价格在50 EMA下方
                ema_spread = (self.ema21 - self.ema9) / self.ema21 * 100
                trend_dist = (self.ema50 - price) / self.ema50 * 100
                strength = min(100, 60 + ema_spread * 1000 + trend_dist * 50 + self.adx)
                
                return Signal(
                    engine=self.name,
                    direction=SignalDirection.PUT,
                    strength=strength,
                    entry_price=price,
                    reason=f"EMA死叉 9<{self.ema21:.2f}, ADX={self.adx:.1f}",
                    metadata={
                        'ema9': self.ema9,
                        'ema21': self.ema21,
                        'ema50': self.ema50,
                        'adx': self.adx,
                    }
                )
                
        return None
        
    def get_state(self) -> Dict:
        """获取引擎状态"""
        state = super().get_state()
        state.update({
            'ema9': self.ema9,
            'ema21': self.ema21,
            'ema50': self.ema50,
            'adx': self.adx,
        })
        return state
        
    def reset(self) -> None:
        """重置"""
        super().reset()
        self.ema9 = None
        self.ema21 = None
        self.ema50 = None
        self.ema9_prev = None
        self.ema21_prev = None
        self._tr_list.clear()
        self._plus_dm.clear()
        self._minus_dm.clear()
        self.adx = 0.0
