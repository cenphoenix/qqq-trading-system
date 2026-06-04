"""Brooks-inspired price action quality checks for intraday entries."""

from typing import Dict, List


class PriceActionFilter:
    """Identify trend continuation and tight-range conditions from recent bars."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def evaluate(self, bars: List[dict], direction: str) -> Dict:
        if len(bars) < 21:
            return {'ready': False, 'allow': False, 'reason': '价格行为K线不足'}

        recent = bars[-21:]
        current = recent[-1]
        previous = recent[-2]
        bull = direction == 'call'

        bar_range = max(float(current['high']) - float(current['low']), 1e-9)
        body = abs(float(current['close']) - float(current['open']))
        prior_bodies = [
            abs(float(bar['close']) - float(bar['open']))
            for bar in recent[-11:-1]
        ]
        avg_body = sum(prior_bodies) / len(prior_bodies)

        if bull:
            directional_bar = current['close'] > current['open']
            close_location = (current['close'] - current['low']) / bar_range
            follow_through = (
                current['close'] > previous['close']
                and previous['close'] > previous['open']
            )
        else:
            directional_bar = current['close'] < current['open']
            close_location = (current['high'] - current['close']) / bar_range
            follow_through = (
                current['close'] < previous['close']
                and previous['close'] < previous['open']
            )

        if bull:
            min_close_location = float(
                self.cfg.get('price_action_call_min_close_location', 0.55)
            )
            min_body_ratio = float(
                self.cfg.get('price_action_call_min_body_ratio', 0.80)
            )
        else:
            min_close_location = float(self.cfg.get('price_action_min_close_location', 0.65))
            min_body_ratio = float(self.cfg.get('price_action_min_body_ratio', 1.0))
        body_ratio = body / max(avg_body, 1e-9)
        strong_breakout = (
            directional_bar
            and close_location >= min_close_location
            and body_ratio >= min_body_ratio
        )

        last_five = recent[-5:]
        direction_bars = sum(
            (bar['close'] > bar['open']) if bull else (bar['close'] < bar['open'])
            for bar in last_five
        )

        last_eight = recent[-8:]
        overlaps = []
        alternations = 0
        for first, second in zip(last_eight, last_eight[1:]):
            overlap = max(
                0.0,
                min(first['high'], second['high']) - max(first['low'], second['low']),
            )
            smaller_range = max(
                min(first['high'] - first['low'], second['high'] - second['low']),
                1e-9,
            )
            overlaps.append(overlap / smaller_range)
            if (first['close'] - first['open']) * (second['close'] - second['open']) < 0:
                alternations += 1

        overlap_ratio = sum(overlaps) / len(overlaps)
        alternation_ratio = alternations / max(len(last_eight) - 1, 1)
        recent_high = max(bar['high'] for bar in recent[-20:])
        recent_low = min(bar['low'] for bar in recent[-20:])
        range_position = (
            (current['close'] - recent_low) / (recent_high - recent_low)
            if recent_high > recent_low
            else 0.5
        )
        tight_range_middle = (
            overlap_ratio >= float(self.cfg.get('price_action_tight_overlap', 0.62))
            and alternation_ratio >= float(self.cfg.get('price_action_tight_alternation', 0.43))
            and 0.33 <= range_position <= 0.67
        )

        min_direction_bars = int(self.cfg.get('price_action_min_direction_bars', 3))
        trend_continuation = (
            (strong_breakout or follow_through)
            and direction_bars >= min_direction_bars
            and not tight_range_middle
        )
        if trend_continuation:
            reason = (
                f"趋势延续: 同向K线{direction_bars}/5, "
                f"强突破={strong_breakout}, 跟进={follow_through}"
            )
        elif tight_range_middle:
            reason = "紧密震荡区间中部"
        else:
            reason = (
                f"缺少趋势延续: 同向K线{direction_bars}/5, "
                f"强突破={strong_breakout}, 跟进={follow_through}"
            )

        return {
            'ready': True,
            'allow': trend_continuation,
            'reason': reason,
            'strong_breakout': strong_breakout,
            'follow_through': follow_through,
            'direction_bars': direction_bars,
            'tight_range_middle': tight_range_middle,
            'overlap_ratio': overlap_ratio,
            'alternation_ratio': alternation_ratio,
            'range_position': range_position,
            'body_ratio': body_ratio,
            'close_location': close_location,
        }

    def market_state(self, bars: List[dict]) -> Dict:
        """Return the current Always-In direction or trading-range state."""
        call = self.evaluate(bars, 'call')
        put = self.evaluate(bars, 'put')
        if not call.get('ready') or not put.get('ready'):
            return {'state': 'warming_up', 'direction': '', 'reason': '价格行为K线不足'}

        if call.get('allow') and not put.get('allow'):
            state = 'always_in_long'
            direction = 'call'
            reason = call.get('reason', '')
        elif put.get('allow') and not call.get('allow'):
            state = 'always_in_short'
            direction = 'put'
            reason = put.get('reason', '')
        elif call.get('tight_range_middle') or put.get('tight_range_middle'):
            state = 'trading_range'
            direction = ''
            reason = '紧密震荡区间'
        else:
            state = 'neutral'
            direction = ''
            reason = '尚无可信单边方向'

        recent = bars[-20:]
        range_high = max(bar['high'] for bar in recent)
        range_low = min(bar['low'] for bar in recent)
        range_height = range_high - range_low
        measured_move_target = 0.0
        if direction == 'call':
            measured_move_target = range_high + range_height
        elif direction == 'put':
            measured_move_target = range_low - range_height

        return {
            'state': state,
            'direction': direction,
            'reason': reason,
            'setups': self.detect_setups(bars),
            'range_high': range_high,
            'range_low': range_low,
            'measured_move_target': measured_move_target,
            'call': call,
            'put': put,
        }

    def detect_setups(self, bars: List[dict]) -> List[Dict]:
        """Detect Brooks setups for context; these are not standalone entry signals."""
        if len(bars) < 24:
            return []

        recent = bars[-24:]
        current = recent[-1]
        signal_bar = recent[-2]
        test_bar = recent[-3]
        reference = recent[-23:-3]
        setups = []

        ref_high = max(bar['high'] for bar in reference)
        ref_low = min(bar['low'] for bar in reference)
        avg_body = sum(
            abs(bar['close'] - bar['open']) for bar in recent[-13:-3]
        ) / 10

        if (
            test_bar['low'] < ref_low
            and self._is_strong_bar(signal_bar, 'call', avg_body)
            and signal_bar['close'] > ref_low
            and current['close'] > signal_bar['high']
        ):
            setups.append({'name': 'Failed_Breakout', 'direction': 'call'})
        if (
            test_bar['high'] > ref_high
            and self._is_strong_bar(signal_bar, 'put', avg_body)
            and signal_bar['close'] < ref_high
            and current['close'] < signal_bar['low']
        ):
            setups.append({'name': 'Failed_Breakout', 'direction': 'put'})

        swing_window = recent[-21:-3]
        swing_lows = [
            idx for idx in range(1, len(swing_window) - 1)
            if swing_window[idx]['low'] < swing_window[idx - 1]['low']
            and swing_window[idx]['low'] < swing_window[idx + 1]['low']
        ]
        swing_highs = [
            idx for idx in range(1, len(swing_window) - 1)
            if swing_window[idx]['high'] > swing_window[idx - 1]['high']
            and swing_window[idx]['high'] > swing_window[idx + 1]['high']
        ]

        if len(swing_lows) >= 3:
            pushes = swing_lows[-3:]
            if (
                swing_window[pushes[0]]['low'] > swing_window[pushes[1]]['low']
                > swing_window[pushes[2]]['low']
                and self._is_strong_bar(signal_bar, 'call', avg_body)
                and current['close'] > signal_bar['high']
            ):
                setups.append({'name': 'Wedge', 'direction': 'call'})
        if len(swing_highs) >= 3:
            pushes = swing_highs[-3:]
            if (
                swing_window[pushes[0]]['high'] < swing_window[pushes[1]]['high']
                < swing_window[pushes[2]]['high']
                and self._is_strong_bar(signal_bar, 'put', avg_body)
                and current['close'] < signal_bar['low']
            ):
                setups.append({'name': 'Wedge', 'direction': 'put'})

        closes = [bar['close'] for bar in recent]
        sma20 = sum(closes[-20:]) / 20
        prior_sma20 = sum(closes[-24:-4]) / 20
        pullback_window = recent[-11:-3]
        pullback_lows = [
            idx for idx in range(1, len(pullback_window) - 1)
            if pullback_window[idx]['low'] < pullback_window[idx - 1]['low']
            and pullback_window[idx]['low'] < pullback_window[idx + 1]['low']
        ]
        pullback_highs = [
            idx for idx in range(1, len(pullback_window) - 1)
            if pullback_window[idx]['high'] > pullback_window[idx - 1]['high']
            and pullback_window[idx]['high'] > pullback_window[idx + 1]['high']
        ]
        if (
            sma20 > prior_sma20
            and len(pullback_lows) >= 2
            and min(bar['low'] for bar in pullback_window) <= sma20 * 1.001
            and self._is_strong_bar(signal_bar, 'call', avg_body)
            and current['close'] > signal_bar['high']
        ):
            setups.append({'name': 'H2', 'direction': 'call'})
        if (
            sma20 < prior_sma20
            and len(pullback_highs) >= 2
            and max(bar['high'] for bar in pullback_window) >= sma20 * 0.999
            and self._is_strong_bar(signal_bar, 'put', avg_body)
            and current['close'] < signal_bar['low']
        ):
            setups.append({'name': 'L2', 'direction': 'put'})

        current_time = current.get('time')
        minute = current_time.hour * 60 + current_time.minute if hasattr(current_time, 'hour') else 0
        if 576 <= minute <= 630:
            opening_bars = [
                bar for bar in bars
                if hasattr(bar.get('time'), 'hour')
                and 570 <= bar['time'].hour * 60 + bar['time'].minute < 575
            ]
            if len(opening_bars) >= 4:
                opening_high = max(bar['high'] for bar in opening_bars)
                opening_low = min(bar['low'] for bar in opening_bars)
                if (
                    test_bar['low'] <= opening_low
                    and self._is_strong_bar(signal_bar, 'call', avg_body)
                    and current['close'] > signal_bar['high']
                ):
                    setups.append({'name': 'Opening_Reversal', 'direction': 'call'})
                if (
                    test_bar['high'] >= opening_high
                    and self._is_strong_bar(signal_bar, 'put', avg_body)
                    and current['close'] < signal_bar['low']
                ):
                    setups.append({'name': 'Opening_Reversal', 'direction': 'put'})

        return setups

    def _is_strong_bar(self, bar: dict, direction: str, avg_body: float) -> bool:
        bar_range = max(bar['high'] - bar['low'], 1e-9)
        body = abs(bar['close'] - bar['open'])
        if direction == 'call':
            directional = bar['close'] > bar['open']
            close_location = (bar['close'] - bar['low']) / bar_range
        else:
            directional = bar['close'] < bar['open']
            close_location = (bar['high'] - bar['close']) / bar_range
        return directional and close_location >= 0.65 and body >= avg_body
