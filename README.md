# QQQ 0DTE 实盘交易系统 v6.5

壮壮交易所 — QQQ 零日到期期权全自动交易系统，基于 Longbridge（长桥）OpenAPI。

## 功能特性

- **Regime 自适应策略**：市场状态检测（trending/choppy/neutral），动态调整 lookback、止损止盈、仓位
- **双路径突破信号**：RSI 预过滤 → 突破检测 → 多级滤镜（SMA20/50、RSI方向、动量、成交量、实体、VWAP、ATR追价、预加载滤镜）
- **实时行情订阅**：通过 Longbridge WebSocket 订阅 QQQ 1 分钟 K 线
- **自动期权交易**：信号触发后自动选择 ATM±$2 合约、下单、止盈止损
- **3 阶段退出**：动态止盈（100%平半仓 + 峰值回撤30%）+ 跟踪止损 + 分阶段超时退出
- **多级熔断**：亏损 5% 警告 → 8% 保守减仓 → 12% 停止交易
- **连亏冷却**：幅度阈值 + 次数上限自动暂停
- **Telegram 通知**：开仓/平仓/熔断/系统事件，HTML 格式化推送
- **Web 仪表盘**：实时查看资金、行情、持仓、信号、滤镜状态

## 快速开始

### 1. 安装依赖

```bash
pip install longbridge openapi
```

> 注意：SDK 版本 ≥0.2.78，需从 GitHub 安装：`pip install git+https://github.com/longbridge/openapi.git`

### 2. OAuth2 认证

系统统一使用 **OAuth2** 认证（废弃了旧版 API Key）。配置 `.env`：

```
LONGBRIDGE_CLIENT_ID=你的ClientID
```

在 [长桥开放平台](https://open.longportapp.com/) 创建应用获取 Client ID。

### 3. 配置交易参数

编辑 `settings.json`，详细参数字段见 `CONFIG.md`。

### 4. 启动系统

```bash
# 启动交易引擎（含 Web 仪表盘）
python3 run_web.py

# 或直接运行交易程序
python3 live_trader.py
```

启动后浏览器访问 `http://localhost:8080` 查看仪表盘。

## 项目结构

```
├── live_trader.py          # 核心交易引擎 v6.5
├── trader_web.py           # Web 仪表盘（Flask API + 前端）
├── run_web.py              # Web 启动器
├── config_manager.py       # 配置管理器
├── update_gist.py          # 交易记录同步到 GitHub Gist
├── watchdog.py             # 看门狗（崩溃自动重启）
├── strategy/
│   ├── filters.py          # FilterEngine 滤镜引擎 + Regime 检测
│   └── options.py          # 期权工具函数
├── data/
│   └── qqq_1min_regular.csv  # 1分钟K线历史数据（回测用）
├── settings.json           # 交易参数配置
├── CONFIG.md               # 配置参数说明文档
├── .env                    # OAuth2 凭据
├── archive/gui/            # 归档的旧GUI文件
├── backtest_v6.py          # 回测引擎（Black-Scholes 定价）
├── backtest_engine.py      # 回测引擎（增强版）
└── backtest_optimization.py # 参数优化脚本
```

## 策略逻辑

### Regime 检测
每根 K 线运行时自动判断市场状态，动态调整参数：

| Regime | lookback | pullback | 特点 |
|--------|---------|----------|------|
| Trending | 3 | 需要回踩 | 趋势确认、允许更大跳空、仓位70% |
| Choppy | 2 | 不需要 | 窄幅交易、更严成交量、更紧止损 |
| Neutral | 3 | 不需要 | 正常模式、仓位40% |

### 信号检测流程
1. RSI 预过滤（避开极端超买超卖）
2. 突破检测（价格突破 N 根高低点）
3. 趋势过滤（SMA20 vs SMA50 方向匹配）
4. RSI 方向确认
5. 动量确认（阳线做多/阴线做空）
6. 成交量确认（>MA20×倍数）
7. K线实体确认（>最小比例）
8. VWAP 位置过滤
9. ATR 追价过滤（避免追高杀低）
10. 预加载滤镜（5/6 项通过）

### 退出逻辑
- **动态止盈**：盈利 100% 平半仓，剩余追踪峰值回撤 30%
- **跟踪止损**：盈利 >10% 后启动，回撤 5% 平仓
- **正股回撤**：正股从高点回撤 0.3% 触发
- **超时退出**：持仓 5/10/15 根 K 线 3 阶段收紧
- **固定止损**：期权价格亏损 25%

### 风控
- 日亏损 5% 警告减仓、8% 保守模式、12% 熔断
- 连亏幅度冷却（5%/8%+ 暂停 2/4 根 K 线）
- 连亏次数上限（3 笔暂停 6 根 K 线）

## 回测结果（2025-01-02 ~ 2026-05-13，341 天）

| 指标 | v6.3 (lookback=5) | v6.5 (lookback=3) |
|:----|:-----------------|:-----------------|
| 信号量 | 5,483 (日均 16.1) | **6,483 (日均 19.0)** |
| 模拟交易 | 3,110 | 3,222 |
| 总盈亏 | **+$1,776** | **+$2,319** |
| 胜率 | 49.3% | 49.3% |
| 盈亏比 (PF) | 1.02 | 1.03 |

> 注：回测使用简化 BS 期权定价模型，SL/TP 基于期权价格 25%/30%，杠杆 4x。v6.5 信号量多 18%，盈亏略高 $543。

## 通知配置

### Telegram

在 `settings.json` 中：

```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "你的BotToken",
    "chat_id": "你的ChatID"
  }
}
```

通知格式：HTML 卡片式，含分隔线和结构化数据，支持开仓/平仓/熔断/系统事件。

## 技术栈

| 组件 | 技术 |
|------|------|
| 交易引擎 | Python, NumPy, SciPy |
| API 通信 | Longbridge OpenAPI (WebSocket + REST, OAuth2) |
| Web 仪表盘 | Flask + HTML/CSS/JS |
| 通知 | Telegram Bot API (HTML parse_mode) |
| 认证 | OAuth 2.0 |

## 注意事项

- 仅供学习和研究，不构成投资建议
- 期权交易风险极高，可能损失全部本金
- 建议先用模拟账户测试（长桥支持模拟盘）
- OAuth2 Token 到期后需重新认证
- 交易时间为美股时段（09:35-15:50 ET），自动处理夏令时/冬令时

## License

MIT
