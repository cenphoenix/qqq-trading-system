"""v7 signal integration layer for live_trader.py."""
import os
import sys
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy.engines import (
    SignalManager,
    Signal,
    OpeningRangeEngine,
    VWAPEngine,
    BollingerEngine,
    EMAEngine,
    RSIDivergenceEngine,
    KlinePatternEngine,
    GranvillePullbackEngine,
    ChanFirstBuyEngine,
    RSIOverboughtEngine,
    MomentumDeathEngine,
    VIXFilter,
)
from signal_names import display_signal_name


class V7Integration:
    """Bridge new v7 engines into the signal format expected by live_trader."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.signal_manager = SignalManager(cfg)

        self.signal_manager.register(OpeningRangeEngine(cfg))
        self.signal_manager.register(VWAPEngine(cfg))
        self.signal_manager.register(BollingerEngine(cfg))
        self.signal_manager.register(EMAEngine(cfg))
        self.signal_manager.register(RSIDivergenceEngine(cfg))
        self.signal_manager.register(KlinePatternEngine(cfg))
        self.signal_manager.register(GranvillePullbackEngine(cfg))
        self.signal_manager.register(ChanFirstBuyEngine(cfg))
        self.signal_manager.register(RSIOverboughtEngine(cfg))
        self.signal_manager.register(MomentumDeathEngine(cfg))

        self.vix_filter = VIXFilter(cfg)
        self.last_signal: Optional[Signal] = None

    def update(self, bar: dict, et_minute: int = 0) -> None:
        """Update all registered engines with the latest bar."""
        self.signal_manager.update(bar, et_minute)

    def update_vix(self, vix: float) -> None:
        """Update VIX filter value."""
        self.vix_filter.update(vix)

    def check_signal(self) -> Optional[Dict]:
        """Check all engines and return the live_trader-compatible signal."""
        if not self.vix_filter.should_trade():
            return None

        signal = self.signal_manager.check()
        if signal is None:
            return None

        self.last_signal = signal
        result = self._convert_signal(signal)

        if result['dir'] == 'put':
            min_strength = 80 if result['regime'] == 'neutral' else 70
            if signal.strength < min_strength:
                return None

        return result

    def _convert_signal(self, sig: Signal) -> Dict:
        """Convert v7 Signal to the signal dict consumed by live_trader."""
        price = sig.entry_price
        dir_str = sig.direction.value
        display_engine = display_signal_name(sig.engine)

        sl_pct = self.cfg.get('sl', 0.25)
        tp_pct = self.cfg.get('tp', 0.30)
        tp_partial = self.cfg.get('tp_partial_pct', 1.0)
        timeout_bars = self.cfg.get('timeout_stage1_bars', 5)

        pos_mult = 1.0
        regime = 'neutral'

        pos_mult *= self.vix_filter.get_position_mult()

        if sig.engine == 'opening_range':
            pos_mult *= 1.1
            regime = 'trending'
        elif sig.engine == 'vwap':
            regime = 'trending'
        elif sig.engine == 'bollinger':
            sl_pct *= 0.9
            regime = 'trending'
        elif sig.engine == 'ema':
            regime = 'trending'
        elif sig.engine == 'rsi_divergence':
            pos_mult *= 0.8
            regime = 'neutral'
        elif sig.engine == 'kline_pattern':
            pos_mult *= 0.8
            regime = 'neutral'
        elif sig.engine == 'granville_pullback':
            pos_mult *= 0.9
            regime = 'trending'
        elif sig.engine == 'chan_first_buy':
            pos_mult *= 0.7
            regime = 'neutral'
        elif sig.engine == 'rsi_overbought':
            pos_mult *= 0.7
            regime = 'neutral'
        elif sig.engine == 'momentum_death':
            pos_mult *= 0.8
            regime = 'trending'

        if dir_str == 'call':
            sl_price = price * (1 - sl_pct)
            tp_price = price * (1 + tp_pct)
        else:
            sl_price = price * (1 + sl_pct)
            tp_price = price * (1 - tp_pct)

        return {
            'dir': dir_str,
            'reason': f'{display_engine}: {sig.reason}',
            'price': price,
            'sl': sl_price,
            'tp': tp_price,
            'sl_pct': sl_pct,
            'tp_partial_pct': tp_partial,
            'timeout_bars': timeout_bars,
            'pos_mult': pos_mult,
            'regime': regime,
            'engine': display_engine,
            'raw_engine': sig.engine,
            'display_engine': display_engine,
            'strength': sig.strength,
            'metadata': sig.metadata,
        }

    def get_engine_states(self) -> list:
        """Return engine states for dashboard."""
        return self.signal_manager.get_engine_states()

    def get_vix_state(self) -> Dict:
        """Return VIX state for dashboard."""
        return self.vix_filter.get_state()

    def reset(self) -> None:
        """Reset all engines for a new trading day."""
        self.signal_manager.reset()
        self.vix_filter.reset()
        self.last_signal = None
