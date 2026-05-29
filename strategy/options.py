"""
期权工具函数
"""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

TZ_ET = ZoneInfo("America/New_York")


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

    # 14:00 ET 之后使用第二天到期的期权（避免当天末日高波动率和保证金）
    from datetime import timedelta
    if now_et.hour >= 14:
        expiry_date = now_et + timedelta(days=1)
    else:
        expiry_date = now_et
    expiry = expiry_date.strftime('%y%m%d')
    symbol = f"QQQ{expiry}{option_type}{strike * 1000:06d}.US"
    return symbol