"""Signal display-name mapping shared by trader and dashboards."""

SIGNAL_DISPLAY_NAMES = {
    'vwap': 'VWAP_Breakout',
    'ema': 'EMA_Cross',
    'rsi_divergence': 'RSI_Reversal',
    'rsi_overbought': 'RSI_Overbought',
    'kline_pattern': 'Kline_Pattern',
    'granville_pullback': 'Granville_Pullback',
    'chan_first_buy': 'Chan_First_Buy',
    'momentum_death': 'Momentum_Death',
    'opening_range': 'Kline_Pattern',
    'bollinger': 'Kline_Pattern',
    'breakout': 'Kline_Pattern',
    'v6.5': 'Kline_Pattern',
    'trending': 'Kline_Pattern',
    'neutral': 'Kline_Pattern',
    'choppy': 'Kline_Pattern',
    'reversal': 'RSI_Reversal',
}


def display_signal_name(name: str, fallback: str = 'Kline_Pattern') -> str:
    if not name:
        return fallback
    return SIGNAL_DISPLAY_NAMES.get(str(name), str(name))
