# QQQ 0DTE 交易系统配置说明

配置位于项目根目录 `settings.json`，系统启动时自动加载。密钥凭据在 `.env` 中。

---

## 📡 信号参数 `signal`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `symbol` | `QQQ.US` | 交易标的，美股代码+.US |
| `rsi_period` | `14` | RSI 计算周期 |
| `rsi_overbought` | `75` | RSI 超买阈值（触及时做空信号优先级降低） |
| `rsi_oversold` | `25` | RSI 超卖阈值（触及时做多信号优先级降低） |
| `lookback` | `3` | Classic 突破检测窗口（N 根 K 线内高低点突破） |
| `lookback_accel` | `2` | Accelerated 加速突破窗口（更短周期，捕捉急涨急跌） |
| `vol_mult` | `0.8` | 成交量倍数阈值：当前量 > MA(volume)×vol_mult 才考虑信号 |
| `min_body` | `0.0003` | K 线最小实体比例（0.03%）。实体太小视为十字星，忽略 |
| `max_gap` | `0.002` | 最大跳空比例（0.20%）。跳空太大跳过本根 K 线 |
| `pullback_confirm` | `false` | 是否需要回踩确认。true=信号 K 线后需回踩才入场 |
| `reversal_drop` | `0.002` | 衰竭反转：当前价距 N 根高点跌幅超过此值（0.2%）触发反转信号 |
| `reversal_bounce` | `0.001` | 衰竭反转：反转 K 线实体需超过此值（0.1%）才算有效 |

### 信号生成机制

系统在每个交易分钟检测 K 线，按以下顺序判定信号：

1. **Regime 检测** — 判断当前市场状态（trending/choppy/squeezing/volatile）
2. **预加载滤镜** — SMA20 方向、成交量、动量、K 线实体、跳空检测
3. **Classic 突破** (`lookback`) — N 根 K 线高点/低点被突破
4. **Accelerated 突破** (`lookback_accel`) — 更短周期的突破
5. **衰竭反转** — 趋势末端反转信号
6. **RSI 过滤** — 超买区做空/超卖区做多加分
7. **Regime 动态参数** — 根据 Regime 调整止损止盈

---

## 💰 风控参数 `risk`

### 仓位管理

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `capital` | `100000` | 账户总资金（$），用于仓位计算。实盘启动后用 SDK 拉取实际值替代 |
| `order_pct` | `8.0` | 单笔开仓资金占比（%）—— 每次开仓用总资金的 8% 购买期权 |
| `contract_multiplier` | `100` | 每张期权合约对应股数（美股标准 = 100） |
| `option_offset` | `2.0` | 行权价偏移（$）。做多 = ATM + 2，做空 = ATM - 2 |
| `vol_adjusted_sizing` | `true` | 是否按波动率调整仓位。高波动自动减仓 |
| `base_atr` | `0.35` | 基准 ATR 值。当实际 ATR > 此值时，自动减少仓位 |

### 止损/止盈

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `sl` | `0.25` | 固定止损：期权价格亏损 25% 时平仓 |
| `tp` | `0.30` | 固定止盈（旧逻辑）：期权价格盈利 30% 时平仓。新策略改用下方动态止盈 |
| `tp_partial_pct` | `1.00` | 部分止盈阈值：盈利 100% 时平掉一半仓位 |
| `tp_trail_drop` | `0.30` | 峰值追踪止盈：从盈利峰值回撤 30% 时全部平仓 |
| `stock_trail_pct` | `0.003` | 正股跟踪止损：正股从持仓期间高点回撤 0.3% 触发退出 |
| `trail_activate` | `0.10` | 跟踪止损激活条件：期权盈利超过 10% 后开始跟踪 |
| `trail_drop` | `0.05` | 跟踪止损回撤比例：从期权价格峰值回撤 5% 时平仓 |

### 超时退出（3 阶段）

持仓如果一直不出止损/止盈，按时间分阶段强制退出：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `timeout_stage1_bars` | `5` | 第 1 阶段：持仓超过 5 根 K 线，要求至少盈利 0.3% |
| `timeout_stage1_min` | `0.3` | 第 1 阶段最低盈利目标（%） |
| `timeout_stage2_bars` | `10` | 第 2 阶段：持仓 10 根 K 线，要求至少盈利 0.6% |
| `timeout_stage2_min` | `0.6` | 第 2 阶段最低盈利目标（%） |
| `timeout_stage3_bars` | `15` | 第 3 阶段：持仓超过 15 根 K 线，强制平仓 |
| `timeout_stage3_min` | `1.0` | 第 3 阶段最低盈利目标（%） |
| `timeout_min_bars` | `6` | 最短持仓 K 线数。在此之前即使触发止损也不退出 |

### 亏损熔断

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `daily_limit` | `12.0` | 日亏损熔断阈值（%）：当日亏损超过 capital×12% 后停止所有交易 |
| `daily_limit_warning_pct` | `5.0` | 警告线：亏损超过 5% 时触发警告提示 |
| `daily_limit_conservative_pct` | `8.0` | 保守线：亏损超过 8% 时降低仓位到 25% |
| `max_trades` | `999` | 每日最大交易次数 |
| `half_close_sl_tighten` | `0.15` | 半仓平仓后止损收紧：平掉一半后，止损缩小到 15% |

### 连亏冷却

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `loss_cooldown_thresholds` | `[5, 8]` | 连亏触发冷却的亏损幅度阈值（%）。超过 5% 冷却 2 根，超过 8% 冷却 4 根 |
| `loss_cooldown_values` | `[2, 4, 8]` | 对应冷却 K 线数：亏 5%→冷却 2 根，亏 8%→冷却 4 根，亏更多→冷却 8 根 |
| `loss_consecutive_limit` | `3` | 连续亏损次数上限：连亏 3 笔后强制冷却 |
| `loss_consecutive_cooldown` | `6` | 连亏强制冷却 K 线数：连亏达到上限后冷却 6 根 K 线 |

---

## ⏰ 交易窗口 `trading`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `start_time` | `09:35` | 允许新开仓的开始时间（美东 ET）。开盘后 5 分钟开始，给市场稳定时间 |
| `end_time` | `14:00` | 允许新开仓的结束时间（美东 ET）。收盘前 2 小时停止新开仓 |
| `check_interval` | `20` | 主循环检测间隔（秒） |
| `post_open_cooldown` | `15` | 开盘后冷却时间（分钟）。开盘后 15 分钟内不开新仓 |
| `loss_cooldown` | `3` | 单笔亏损后同方向冷却 K 线数 |

**实际运行逻辑：**
- `start_time` ~ `end_time`：允许新开仓
- 已有持仓持续监控到 **16:00 ET**（强制平仓）
- 美东时间自动根据夏令时/冬令时转换

---

## 🔔 通知 `telegram` / `feishu`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | 见下文 | 是否启用该通知渠道 |
| `bot_token` | 空 | Telegram Bot Token（从 BotFather 获取） |
| `chat_id` | 空 | Telegram 聊天 ID |
| `open_id` | 空 | 飞书 Open ID |

系统默认启用 `telegram`，推送内容包括：
- 开仓/平仓通知
- 止损/止盈触发
- 熔断激活
- 每日盈亏汇总

---

## 🔑 密钥配置 `.env`

```
LONGBRIDGE_CLIENT_ID=xxx         # 长桥 OAuth2 Client ID（必填）
GIST_ID=xxx                      # GitHub Gist ID（可选，用于同步状态）
GITHUB_TOKEN=ghp_xxx             # GitHub Token（可选）
```

旧版 `LONGBRIDGE_APP_KEY/SECRET/ACCESS_TOKEN` 已废弃，系统统一使用 **OAuth2** 认证。
