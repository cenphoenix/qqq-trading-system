# QQQ 0DTE Trading System v7

当前主线是 v7 实盘交易系统：`run_web.py` 同时启动 Web 仪表盘和交易引擎。

## 快速启动

第一次配置 Windows 虚拟环境：

```powershell
cd D:\Github\qqq-trading-system
C:\Users\Chris\AppData\Local\Programs\Python\Python312\python.exe -m venv .venv-win
.\.venv-win\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

日常启动：

```powershell
cd D:\Github\qqq-trading-system
.\.venv-win\Scripts\activate
python run_web.py
```

也可以双击 `start.bat`。

Web 仪表盘地址：

```text
http://localhost:8080
```

## 当前主线文件

- `run_web.py`：当前入口，启动 Dashboard 和交易引擎。
- `live_trader.py`：实盘交易主程序。
- `dashboard_v7.py`：v7 Web 仪表盘。
- `config_manager.py`：配置和 `.env` 加载。
- `settings.json.example`：配置模板。
- `v7_integration.py`：v7 多引擎信号集成层。
- `strategy/`：当前策略、过滤器、期权符号和多信号引擎。
- `update_gist.py`：交易记录同步工具。
- `fetch_historical_kline.py`：下载历史K线，供后续回测/分析使用。
- `watchdog.py`：可选守护脚本，用于交易引擎异常重启。

## 运行数据

这些文件/目录是运行时数据，默认不提交：

- `.env`
- `settings.json`
- `state.json`
- `longbridge_orders.json`
- `today.csv`
- `logs/`
- `records/`
- `data/`
- `.venv-win/`

## 已归档内容

历史 v4/v5/v6/v6.3/v6.5 脚本、旧 Flask 仪表盘、旧回测脚本和旧批处理文件已移动到：

```text
archive/legacy/
```

这些文件不参与当前 v7 实盘启动，只作为参考或回滚材料保留。

## 当前策略说明

v7 主线仍保留部分 v6.3/v6.5 的成熟逻辑：

- `FilterEngine` 继续提供 VWAP、SMA、MACD、ATR、市场状态等过滤数据。
- `V7Integration` 注册 opening range、VWAP、Bollinger、EMA、RSI divergence 等多引擎信号。
- `live_trader.py` 负责统一风控、下单、持仓监控、平仓和记录。
- 入场后会记录第 5 / 10 / 20 根 1 分钟K线的方向收益，便于后续分析信号质量。

