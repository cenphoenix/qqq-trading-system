# QQQ 0DTE Trading System v7

基于长桥 OAuth 的 QQQ 日内期权交易与复盘系统。当前生产入口为
`run_web.py`，它在同一进程中启动交易引擎和 FastAPI Dashboard。

## 快速启动

首次创建 Windows 虚拟环境：

```powershell
cd D:\Github\qqq-trading-system
C:\Users\Chris\AppData\Local\Programs\Python\Python312\python.exe -m venv .venv-win
.\.venv-win\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

日常启动：

```powershell
.\.venv-win\Scripts\activate
python run_web.py
```

也可以双击 `start.bat`。Dashboard 默认使用配置的端口；端口占用时会自动选择备用端口，并在控制台输出实际地址。

## 生产代码

- `run_web.py`：唯一生产入口。
- `live_trader.py`：交易、持仓、风控、通知和交易账本。
- `dashboard_v7.py`：FastAPI/WebSocket Dashboard。
- `config_manager.py`：配置加载、默认值与校验。
- `settings.json.example`：可提交的配置模板。
- `v7_integration.py`：v7 多信号引擎适配层。
- `strategy/`：信号引擎、价格行为、指标过滤和期权代码选择。
- `review_summary.py`：日、周、月复盘摘要。
- `signal_names.py`：统一信号名称。
- `update_gist.py`：复盘数据同步工具。

## 分析工具

- `analyze_signal_probes.py`：分析信号触发后 5/10/20 根 K 线表现。
- `analysis/replay_v62_call_pool.py`：回放 v6.2 CALL 信号池。
- `fetch_historical_kline.py`：下载历史 K 线。
- `tools/rebuild_record_from_orders.py`：按成交顺序重建交易周期账本。
- `tools/audit_python.py`：检查 Python 解析错误、重复定义和明显无用导入。

## 本地运行数据

以下内容默认不提交 Git：

- `.env`、`settings.json`
- `.venv-win/`、`__pycache__/`
- `state.json`、`longbridge_orders.json`、`today.csv`
- `data/`、`records/`、`logs/`、`reports/`

实盘数据和历史 K 线是策略复盘依据，清理代码时不要删除。

## 维护约定

- 新功能只接入 v7 入口，不再增加带版本号的平行交易主程序。
- 废弃代码由 Git 历史保留，不在仓库中长期维护副本或 backup 文件。
- 策略调整先使用实盘记录与历史 K 线验证，再修改默认配置。
- 提交前运行：

```powershell
python -m compileall -q -x "(.venv-win|.git)" .
python tools/audit_python.py
```
