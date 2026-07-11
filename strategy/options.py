"""
期权工具函数
"""
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ_ET = ZoneInfo("America/New_York")


def _observed_date(month: int, day: int, year: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    cur = date(year, month, 1)
    while cur.weekday() != weekday:
        cur += timedelta(days=1)
    return cur + timedelta(days=7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    cur = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while cur.weekday() != weekday:
        cur -= timedelta(days=1)
    return cur


def _easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _market_holidays(year: int) -> set[date]:
    return {
        _observed_date(1, 1, year),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_date(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_date(6, 19, year),
        _observed_date(7, 4, year),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_date(12, 25, year),
    }


def _is_market_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _market_holidays(day.year)


def _next_market_day(day: date) -> date:
    while not _is_market_day(day):
        day += timedelta(days=1)
    return day


def get_option_symbol(stock_price: float, direction: str, offset: float = 2.0) -> str:
    """
    生成期权合约代码（确保OTM虚值期权）

    stock_price: 正股价格
    direction: 'call' 或 'put'
    offset: 行权价偏移（±$2）

    返回: 期权合约代码，如 'QQQ260422C656000.US'

    行权价计算逻辑（OTM保证）:
    - Call: strike = floor(stock + offset) → 行权价 ≤ stock+offset，始终 > stock（虚值）
    - Put:  strike = ceil(stock - offset)  → 行权价 ≥ stock-offset，始终 < stock（虚值）
    """
    now_et = datetime.now(TZ_ET)

    if direction == 'call':
        strike = math.floor(stock_price + offset)
        option_type = 'C'
    else:
        strike = math.ceil(stock_price - offset)
        option_type = 'P'

    if direction == 'call' and strike <= stock_price:
        strike = int(stock_price) + 1
    elif direction == 'put' and strike >= stock_price:
        strike = int(stock_price) - 1

    # 14:00 ET 之后使用下一个交易日到期，避免周末/节假日生成无效合约
    expiry_day = now_et.date() + timedelta(days=1) if now_et.hour >= 14 else now_et.date()
    expiry_date = _next_market_day(expiry_day)
    expiry = expiry_date.strftime('%y%m%d')
    symbol = f"QQQ{expiry}{option_type}{strike * 1000:06d}.US"
    return symbol


def is_option_expiring_on(symbol: str, target_date: date) -> bool:
    """Return whether a standard QQQ option symbol expires on target_date."""
    if not symbol or not symbol.startswith("QQQ") or len(symbol) < 9:
        return False
    try:
        expiry = datetime.strptime(symbol[3:9], "%y%m%d").date()
    except (TypeError, ValueError):
        return False
    return expiry == target_date
