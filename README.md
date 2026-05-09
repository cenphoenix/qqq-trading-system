# QQQ 0DTE 实盘交易系统 v6.2

热血青年交易所 — QQQ 零日到期期权全自动交易系统

## 功能特性

- **双路径突破策略**：经典模式 + 加速模式，自动适应不同市场节奏
- **实时行情订阅**：通过 Longbridge API WebSocket 订阅 QQQ 1 分钟 K 线
- **自动期权交易**：信号触发后自动选择合约、下单、止盈止损
- **动态止盈**：盈利 100% 平半仓，剩余追踪最高点回撤 30% 全平
- **分阶段超时退出**：持仓时间越长，止损越紧，避免深度套牢
- **VIX 波动率过滤**：根据 VIX 水平动态调整仓位和策略
- **飞书通知**：开仓/平仓/异常实时推送
- **看门狗崩溃恢复**：进程异常退出后自动重启

## 界面

- **桌面控制面板**（Tkinter）：系统托盘常驻，实时监控
- **Web 仪表盘**：浏览器访问，支持手机查看
- **控制面板**：在线调整交易参数，无需重启

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入你的 Longbridge API 密钥：

```bash
cp .env.example .env
```

编辑 `.env`：

```
LONGPORT_APP_KEY=你的AppKey
LONGPORT_APP_SECRET=你的AppSecret
LONGPORT_ACCESS_TOKEN=你的AccessToken
```

获取密钥：https://open.longportapp.com/

### 3. 配置交易参数

编辑 `settings.json` 调整信号过滤、风险管理、交易时间等参数。

### 4. 启动系统

```bash
# 桌面版（推荐，含系统托盘）
python main_app.py

# 或使用启动脚本
start.bat
```

启动后浏览器访问 `http://localhost:8080` 查看 Web 仪表盘。

## 项目结构

```
├── main_app.py          # 主入口（Tkinter GUI + 系统托盘）
├── live_trader.py       # 核心交易引擎 v6.2
├── trader_web.py        # Web 仪表盘
├── dashboard_web.py     # 轻量 Web 仪表盘（无依赖）
├── dashboard_tk.py      # Tkinter 桌面仪表盘
├── config_manager.py    # 配置管理器
├── settings_gui.py      # 设置界面
├── update_gist.py       # 交易记录同步到 GitHub Gist
├── watchdog.py          # 看门狗（崩溃自动重启）
├── backtest_v6.py       # 回测引擎（Black-Scholes 定价）
├── settings.json        # 交易参数配置
├── requirements.txt     # Python 依赖
└── .env.example         # 环境变量模板
```

## 策略逻辑

### 信号检测

在 1 分钟 K 线上检测突破信号：

- **经典路径**：价格突破 N 周期高低点 + 放量确认
- **加速路径**：价格斜率突变检测，响应更敏捷

### 过滤条件

- RSI 超买/超卖过滤
- SMA20/SMA50 趋势过滤
- 跳空幅度过滤
- 成交量确认

### 风控

- 单笔止损：25%
- 每日亏损上限：25%（熔断）
- 动态止盈：100% 平半仓 + 追踪止盈
- 分阶段超时：5/10/15 根 K 线逐步收紧

## 注意事项

- 本系统仅供学习和研究，不构成投资建议
- 期权交易风险极高，可能损失全部本金
- 建议先用模拟账户充分测试
- 实盘使用前请仔细检查所有参数配置
- 系统依赖 Longbridge API，确保网络稳定

## 技术栈

| 组件 | 技术 |
|------|------|
| 交易引擎 | Python, NumPy, SciPy |
| 桌面 GUI | Tkinter, pystray |
| Web 服务 | Python http.server / Flask |
| API 通信 | Longbridge OpenAPI (WebSocket + REST) |
| 通知 | 飞书 Webhook |
| 打包 | PyInstaller |

## License

MIT
