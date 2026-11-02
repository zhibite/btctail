# PolyTail · BTC 尾盘双策略（15m 冻结 + 5m Agent 调优）

在 Polymarket 的 BTC Up/Down 市场做「尾盘热门方」策略，一个进程同时跑两个策略：

| 策略 | 默认模式 | 配置 | 定位 |
|---|---|---|---|
| **BTC 15m** | **实盘** | 最后 60 秒 / **0.88** / 10 秒确认 / 上限 0.94 / 10 USDC | 8–10 月实验后把阈值从 0.90 调到 0.88，成交约 +64% |
| **BTC 5m** | **纸面** | 最后 20 秒 / 0.90 / 无确认 / 上限 0.92 / 10 USDC | Agent 持续调优（文档显示 5m 核心规则是滑点陷阱，只有短窗口小口袋） |

两个策略默认都先尝试 **GTD 挂单**（maker，费率 0），未成交再按配置回退 FAK。

规则原文见 [`docs/late_window_strategy_summary.md`](docs/late_window_strategy_summary.md)。
两个策略各有一套窗口调度、市场发现、风险计数和参数，共享同一个钱包、数据库和面板。

---

## 1. 一分钟跑起来

```bash
cd PolyTail/BTC15Tail
python -m pip install -r requirements.txt      # 或 pip install -e ".[dev,agent]"

cp .env.example .env       # 必须设置 WEB_PASSWORD，否则面板无法登录
python -m src.main
```

打开 <http://127.0.0.1:8013>，登录后看到 15m 与 5m 两张策略卡。

**默认模式：15m 实盘、5m 纸面**。两种情况会自动降级为纸面并在面板提示：

- 未配置 CLOB 凭证（进程不会崩溃，只会把实盘策略降级为纸面）
- 面板上手动切换失败（例如切换时有未结算持仓）

所有页面（含总览和交易记录）都需要登录；没有 `WEB_PASSWORD` 时面板整体不可访问。

命令行研究（两个周期都支持）：

```bash
python -m src.research.cli dataset
python -m src.research.cli backtest --period 15m --start 2026-08-01 --end 2026-10-31
python -m src.research.cli backtest --period 5m  --start 2026-08-01 --end 2026-10-31 \
  --entry-window-sec 30 --confirm-sec 0 --max-entry-price 0.92
python -m src.research.cli validate --period 5m --start 2026-11-01 --end 2026-12-31
```

---

## 2. 策略规则（与回测逐条对应）

| 规则 | 15m | 5m |
|---|---|---|
| 只看窗口最后 N 秒 | 60 | 20 |
| 任一侧首次达到阈值即触发 | 0.88 | 0.90 |
| 等待确认秒数 | 10 | 0 |
| 确认时价格仍 ≥ 阈值，且 **严格小于** 上限 | 0.94 | 0.92 |
| 单笔金额 | 10 USDC | 10 USDC |
| 下单方式 | GTD 优先，未成交回退 FAK | 同左 |
| 买入后持有到结算 | ✓ | ✓ |

细节：

- **每个策略每个窗口最多一笔**（数据库唯一约束 `UNIQUE(slot, period, mode)`）。
- **确认失败即放弃本窗口**，不会在同一窗口反复尝试（与回测一致）。
- **确认时价格 ≥ 上限直接跳过**：这是文档中 15m 的 0.94 优于 0.95 的核心原因。
- **执行前二次校验**：确认到发单之间如果价格冲过上限、或跌回阈值下方 2pp 以上，放弃入场。
- **下单方式**：默认在信号价挂 **GTD/GTC 限价买单**（按单笔金额÷限价换算股数，最少 5 股；post-only，maker 费率 0），
  最多等 `GTD_FILL_TIMEOUT_S`（默认 3 秒）。有成交就取消剩余、按成交量记账；
  完全没成交则回退 **FAK**（限价 = 信号价 + `EXEC_SLIPPAGE`），或按配置放弃本窗口。
- **FAK 卖价高于限价不发送**：如果盘口 ask 已经高于限价，直接放弃并记录 `ask_above_limit`
  （避免发一张必然不成交的单）。GTD 挂单不要求 ask ≤ 限价，因为它是被动等成交。

费用与盈亏（与回测一致）：

```
entry_fill = 信号价 + slippage
shares     = stake / entry_fill
fee        = shares × fee_rate × entry_fill × (1 − entry_fill)      # 只在入场收取
pnl        = shares × payout(1/0) − stake − fee
```

盈亏平衡胜率恰好等于入场价，策略的全部收益来自「实际胜率 − 平均入场价」。

---

## 3. 回测结果对齐文档

15m（官方费用 + 0.005 滑点，2026-08-01 至 10-31）：

| 版本 | 笔数 | 胜率 | 平均入场 | 价差 | 净利 | ROI | t |
|---|---:|---:|---:|---:|---:|---:|---:|
| 文档 0.94 | 248 | 97.18% | 0.9224 | +4.94pp | +106.01 | +4.27% | 3.75 |
| **本项目复现** | **248** | **97.18%** | **0.9224** | **+4.94pp** | **+106.01** | **+4.27%** | **3.75** |
| 文档 0.95 | 390 | 96.67% | 0.9305 | +3.62pp | +113.23 | +2.90% | 2.97 |
| 本项目复现 | 390 | 96.67% | 0.9305 | +3.62pp | +113.23 | +2.90% | 2.97 |
| 文档 6 个月 0.95 | 1,208 | 95.03% | 0.9296 | +2.08pp | +149.47 | +1.24% | 1.85 |
| 本项目复现 | 1,208 | 95.03% | 0.9296 | +2.08pp | +149.47 | +1.24% | 1.85 |

实盘默认阈值已按 8–10 月进场实验改为 **0.88**（见 [`docs/experiments_15m.md`](docs/experiments_15m.md)）：
407 笔、+161.13U、ROI +3.96%、t=3.88，8/9/10 月全部为正。下表仍是文档基线（阈值 0.90）的复现。

5m（同一区间，最后 30 秒 / 0.92 上限 / 无确认）：

| 版本 | 笔数 | 净利 | ROI | t |
|---|---:|---:|---:|---:|
| 文档 | 1,338 | +138.94 | +1.04% | 1.35 |
| 本项目 | 1,337 | +149.00 | +1.11% | 1.46 |

> 5m 的「最后 10/15/20 秒」在文档里是**入场时间子集**，不是更短的入场窗口。
> 本项目把窗口长度本身也作为可调参数，交给 Agent 搜索。

> 文档提醒：15m 的 8–10 月是事后挑选的强区间，完整 6 个月只有 +1.24%、5 月为负。
> 5m 整体显著性不足（t≈1.3–1.5），只能用更严格的标准（见 7.6）。

---

## 4. 架构

```
src/
├── config.py                 # 全局配置 + 每策略 Settings 克隆 + 5m 默认值
├── main.py                   # 启动两个 Runtime + ResearchService + 面板
├── runtime.py                # 单个策略：窗口 → 信号 → 执行 → 结算
├── market/                   # Gamma 发现 / CLOB 行情 / 结算解析 / WS
├── strategy/
│   ├── late_favorite.py      # 状态机 + 费用/结算数学 + 数组回放
│   └── risk.py               # 按 (策略, 模式) 独立的日亏损/连亏/余额/急停
├── execution/
│   ├── clob_client.py        # py_clob_client_v2 异步封装（FAK 买/卖）
│   └── broker.py             # 纸面 / 实盘统一接口
├── persistence/db.py         # SQLite：按 period 分区的窗口/信号/交易/配置
├── research/                 # 数据集、回测、优化、Agent、采集、后台任务
└── web/                      # FastAPI 面板（双策略）
```

关键设计：

1. **每个策略一个 `Settings` 克隆 + 一个 `Runtime`**：窗口长度、参数、单笔金额、
   实盘/纸面模式全部独立；共享 HTTP 客户端、数据库和钱包。
2. **策略是纯状态机**：实盘 `on_tick`，回测 `evaluate_window_array`，
   `tests/test_strategy.py` 用随机序列把两者钉死。
3. **信号价用 CLOB midpoint**（回测同款序列）；盘口 bid/ask 只用于执行判断。
4. **永远没有挂单**：入场是 FAK taker 单，进程崩溃不会留下孤儿订单。
5. **后台化网络调用**：市场发现、结算、余额刷新都不阻塞最后 60/30 秒的 tick 循环。
6. **执行失败防孤儿仓**：POST 报错后检查钱包，若已出现持仓则记账并暂停该策略等待人工核对。

### 数据库

- SQLite：`data/polytail.db`；`windows / signals / trades` 都有 `period` 列。
- 旧版单策略数据库在启动时会自动重命名为 `*_legacy_<ts>` 并重建新表（保留可查）。
- 运行时参数覆盖按策略存 `settings(scope, key)`。

---

## 5. 实盘接入

1. 准备钱包与 CLOB 凭证（两策略共用一个钱包）：
   - 方式 A：`API_KEY` / `API_SECRET` / `API_PASSPHRASE`
   - 方式 B：`WALLET_PRIVATE_KEY`（启动时自动派生 L2 凭证）
   - Magic/邮箱或 Safe 钱包：`SIGNATURE_TYPE=1/2` + `FUNDER_ADDRESS`
2. 先用 `TRADING_MODE=paper` 跑几天，在「交易记录」页按周期核对滑点与拒绝原因。
3. 模式默认已经是 **15m 实盘 / 5m 纸面**，可以在网页「参数」页按策略切换
   （写入数据库，重启后保留）。切换会拒绝有未结算持仓的策略。
4. **5m 默认单笔 `ENTRY_NOTIONAL_USD_5M=10`**。建议先用纸面或小仓位实盘验证信号、成交回报、
   结算记账，再逐步放大；切换前确认钱包余额能覆盖 15m + 5m 的同时持仓。
5. 必须设置 `WEB_PASSWORD`（所有页面都要登录）。

风控（每个策略独立计数，纸面/实盘也不混）：

| 保护 | 默认 | 说明 |
|---|---|---|
| 单日亏损上限 | 50 U | 该策略当日已实现亏损达到即停止当日交易 |
| 连续亏损暂停 | 3 次 → 60 分钟 | 暂停到期或手动恢复后，只有**新**亏损才再次触发 |
| 最低钱包余额 | 15 U | 共享钱包低于该值不再开仓 |
| 买卖价差上限 | 0.06 | 入场瞬间价差过宽则放弃 |
| 急停 / 暂停 | 面板按钮 | 按策略持久化，重启后仍有效；只阻止新开仓 |

---

## 6. 面板

| 页面 | 内容 |
|---|---|
| `/` 总览 | 两个策略并排：倒计时、阶段、UP/DOWN、触发/确认、持仓、风控、统计、盈亏曲线、最近交易；下方合并信号 |
| `/trades` 交易记录 | 按周期/模式筛选，信号价 / 成交价 / 滑点 / 费用 / 结算，未赎回提醒 |
| `/config` 参数 | 策略标签页切换；每策略独立参数、恢复默认、急停/暂停；共享钱包余额 |
| `/research` 研究 | 策略标签页切换；单组回测、训练/验证搜索、Agent 调参、报告与历史运行 |

**所有页面和接口都需要登录**（`/api/health` 除外，供 Coolify 健康检查）。
`WEB_PASSWORD` 为空时无法登录，面板等于关闭。

---

## 7. 研究 / 参数 Agent

### 7.1 回测

```bash
python -m src.research.cli backtest --period 15m --start 2026-08-01 --end 2026-10-31
python -m src.research.cli backtest --period 5m --start 2026-08-01 --end 2026-10-31 \
  --entry-window-sec 30 --threshold 0.90 --confirm-sec 0 --max-entry-price 0.92
```

跨资产检验：用 **BTC 15m 的当前参数**回测 ETH 15m，默认同时跑 BTC 基线并做对比：

```bash
python scripts/backtest_eth15m.py
python scripts/backtest_eth15m.py --start 2026-08-01 --end 2026-10-31
python scripts/backtest_eth15m.py --slippage 0.01 --no-compare
```

结果写入 `data/research/eth_15m_btc_params/`（`report.md`、`summary_*.json`、`trades_*.json`）。

### 7.2 样本外搜索

```bash
python -m src.research.cli optimize --period 5m --iterations 40 --objective profit_dd
```

- 默认按日期顺序 **前 70% 训练 / 后 30% 验证**；也可用 `--train-end` / `--validate-start` /
  `--validate-end` 指定。训练集结束必须早于验证集开始，否则直接报错。
- 搜索空间是有限网格；请求的候选数超过空间大小时自动截断，并有硬性上限 500。
- 只有训练集前 4 名（外加基线）会进入验证集。
- 推荐必须满足：验证集净利 > 0 且验证笔数 ≥ max(5, 最少交易数/4)。
- 搜索空间：
  - 15m：窗口 30–120s、阈值 0.86–0.95、确认 0–20s、上限 0.90–0.97
  - 5m：窗口 10–90s、阈值 0.80–0.94、确认 0–15s、上限 0.85–0.96
    （实盘观察：5m 常在最后 30 秒直接跳到 0.985 以上，需要更早触发才能落在 0.92 上限内）

### 7.3 冻结规则样本外检验

```bash
python -m src.research.cli validate --period 15m --start 2026-11-01 --end 2026-12-31
```

用**当前运行时参数**回测新数据（不做搜索），检查：样本量、净收益为正、t ≥ 1.5、价差为正。
全部通过返回 0，否则返回 1。未通过时的正确动作是减小仓位或暂停，而不是继续调参。

### 7.4 Agent

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python -m src.research.cli agent --period 5m --goal "在控制回撤的前提下最大化净收益"
```

或在 `/research` 页面选 **5m** 后点「运行 Agent」。

Agent 的可调用工具只有：

| 工具 | 作用 |
|---|---|
| `dataset_overview` | 数据范围与规模 |
| `run_backtest` | 单组参数回测 |
| `run_search` | 确定性训练/验证搜索 |
| `validate_params` | 指定区间样本外验证 |
| `finalize` | 提交最终建议 |

硬性约束（代码层面，不只是提示词）：

- 每次运行回测次数有上限（`AGENT_MAX_BACKTESTS`）。
- `finalize` 选择 `apply_candidate` 时必须满足以下之一，否则工具直接拒绝：
  - 参数来自 `run_search` 排行榜，且在搜索自带的验证集上有 ≥ 5 笔交易；或
  - 被 `validate_params` 在一个**与本次会话已回测/搜索过的区间不重叠**的日期上验证过，
    且 ≥ 5 笔交易。
- 建议只包含 `TUNABLE_SPECS` 中的键；应用参数是面板上的独立操作，Agent 不能自己改实盘。
- 系统提示词按周期生成：5m 会强调滑点陷阱、小样本和 0.01 滑点敏感性；
  15m 会强调冻结与样本外验证。

Agent 使用 `claude-opus-5-5`（`AGENT_MODEL` 可改），开启自适应思考与 `effort=high`，
流式请求，system prompt 命中 prompt cache。

### 7.5 尾盘进场机会实验（15m / 5m）

针对「进场太少」「如何提高成交」的问题，实验脚本会一次性对比多种规则变体
（降阈值、软确认、重新武装、超上限后等回撤、加宽窗口等）。

```bash
# 15m：默认 2026-08-01 ~ 10-31
python -m src.research.experiments_late_window
python -m src.research.experiments_late_window --variants baseline,thr_088,soft_088,rearm

# 5m：默认 2026-07-01 ~ 09-30，变体目录按 5m 窗口长度设计
python -m src.research.experiments_late_window --period 5m
python -m src.research.experiments_late_window --period 5m --sweep
```

结论与完整表格：

- 15m：[`docs/experiments_15m.md`](docs/experiments_15m.md)。阈值 0.88 把验证集成交从 56 笔
  提高到 104 笔、净利 +50（基线 +29）；「等回撤」和加宽窗口都会稀释价差。
- 5m：[`docs/experiments_5m.md`](docs/experiments_5m.md)。6–10 月：20 秒窗口 +404U / ROI +1.35% / t=2.67
  （0.01 滑点 +248.6）；30 秒基线 +308.7 / t=1.61（0.01 滑点 +78.3）。收益很薄、9 月为负，继续纸面。

### 7.6 从实盘数据学习

每个策略每个窗口最后 `COLLECT_SEC`（默认 300）秒按 **1 秒**采样，按周期写入：

```
data/dataset/asset=btc/period=15m/date=.../klines_fine.csv
data/dataset/asset=btc/period=5m/date=.../klines_fine.csv
```

格式与历史导出一致，因此面板里把「数据源」切到「实盘采集」即可用**自己的成交环境**
回测和调参——这是校准「信号价 + 实际滑点」与回测假设差异的关键数据。

### 7.7 建议节奏

1. **15m**：不改参数。每月跑一次 `validate`；不通过就降仓位/暂停。
2. **5m**：先纸面或 1 USDC 实盘，累计 2–4 周采集数据后，用「实盘采集」数据源跑一次
   `optimize` / Agent。只有验证集净利为正、t ≥ 2、且 0.01 滑点下仍不为负，才考虑应用。
3. Agent 用来探索机制层面的组合（窗口长度 × 确认 × 上限），不是用来在噪声里抠 0.1%。
4. 任何改动后至少观察一个完整周期再继续调整。

---

## 8. 费用口径与滑点

- 官方公式：`fee = 份额 × 费率 × p × (1−p)`，只在入场收取；结算价 p=1/0 时无退出费。
- 费率优先级：Gamma 市场字段 `feeRate`（若存在） > `TAKER_FEE_RATE`（默认 0.07）。
- `/fee-rate` 的 `base_fee` 单位存在歧义，`FEE_RATE_AUTODETECT` 默认关闭。
- 面板记录每笔的 `entry_price − signal_price`、入场时 ask/bid、限价内深度，
  可直接与 0.005 / 0.01 的滑点假设对比。

---

## 9. 结算与赎回

- 窗口结束后每 15 秒查询一次 Gamma 结算（`/markets?condition_ids=` 失败时回退 `/events?slug=`）。
- 赢了的仓位需要在 Polymarket 网站赎回；未赎回会在「交易记录」页提示，赎回后点「已赎回」。
- 可选：`scripts/redeem.py`（需要 `web3` + MATIC gas）可链上批量赎回，默认只打印不发送。

---

## 10. 部署

```bash
python -m src.main            # 本地
docker compose up -d --build  # Docker
```

- 只用 paper 时不需要任何密钥。
- 实盘密钥通过环境变量注入，不要写进镜像。
- `data/` 需要持久化（SQLite + 采集数据 + 日志），compose 已挂载 `./data:/app/data`。

---

## 11. 测试

```bash
python -m pytest -q
```

覆盖：策略状态机、实盘/回测一致性（随机序列）、费用与 PnL、数据集加载与切片、
端到端回测、优化器的训练/验证切分与终止性、风控（按策略/模式隔离）、配置校验
（含 NaN）、运行时执行链路、采集格式、Agent 工具与样本外护栏、Web API 与鉴权。

---

## 12. 已知限制与注意事项

1. **实盘 API 路径未在本机联调**：构建环境无外网。调用形状照搬 `02.polygtd` 的生产实现，
   仍建议先用 1 USDC 试跑一轮。
2. **1 秒 K 线不是订单簿**：回测用 mid，真实成交在 ask 侧。滑点假设 0.005 约为半个价差，
   实际可能更差；5m 对滑点尤其敏感。
3. **5m 的统计证据弱**：文档中 5m 最好版本的 t 只有约 1.3–1.5，短窗口子集样本更小。
   请把 5m 视为研究/小仓位策略，不要用 15m 的信心对待它。
4. **赎回是手动步骤**（或可选脚本）。未赎回不代表亏损，但会占用资金。
5. **多实例**：同一钱包不要同时跑两个进程；数据库与急停状态也不共享。
6. **时间基准**：窗口按 UTC unix 秒对齐。
7. **采样缺口**：回测确认取「≤ 触发+确认秒 的最后一个采样」，实盘取「≥ 到期时刻的第一个 tick」；
   数据缺秒时回测可能比实盘略乐观，以实盘采集数据的回测为准。
8. **研究任务与交易共用一个进程**：CPU 紧张时后台回测仍可能影响轮询；生产建议低峰跑研究。
9. **GTD 可能部分成交或完全不成交**：挂单 3 秒内没成交会回退 FAK 或放弃；
   部分成交按实际股数记账。maker 成交按 `MAKER_FEE_RATE`（默认 0）计费，FAK 按 `TAKER_FEE_RATE`。
10. **实盘降级**：请求 live 但缺少凭证时，进程会以纸面继续运行并在面板标注
    「凭证缺失·纸面」，不会崩溃；配好凭证后重启或直接在面板切换。
