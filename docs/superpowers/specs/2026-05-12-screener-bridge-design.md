# Screener-Bridge: 四层股票筛选管线设计

> 状态: draft | 日期: 2026-05-12 | 作者: cryilggg

## 1. 概述

### 1.1 背景

Chrysantha 目前通过 `trading-bridge` 调用 TradingAgents-CN 对持仓个股做深度多智能体分析，但缺少上游的**市场扫描 → 候选池缩减**流程。所有标的直接进入 LLM 分析，导致：
- 无市场环境判断，熊市中仍然激进推荐
- 无板块聚焦，分析范围过散
- 无量化初筛，LLM 分析低质量标的浪费 token

### 1.2 目标

新增 `screener-bridge` 服务，在 TradingAgents-CN 之前提供四层筛选管线：

1. **宏观叙事/大盘判断** — 每日盘前输出市场环境分数
2. **板块轮动/资金流向** — 找本周最强 3~5 个板块
3. **个人偏好过滤** — YAML 规则引擎排除黑名单+风控
4. **候选股票池筛选** — 量价指标初筛到 10~30 只

### 1.3 设计决策

| 决策 | 选择 | 依据 |
|------|------|------|
| 集成方式 | 独立 FastAPI 服务 | 与现有微服务架构一致 |
| 触发方式 | 混合（APScheduler + REST API） | 盘前自动 + 盘中按需 |
| 市场覆盖 | A股 + 港股 + 美股 | 一次到位 |
| Vibe-Trading 复用 | 库级 import | 复用 loader/因子逻辑，避免重写 |
| 中间状态 | Redis 缓存（复用现有） | 跨层共享，设置 TTL |

## 2. 架构

### 2.1 目录结构

```
tools/screener-bridge/
├── server.py                  # FastAPI 入口 + 生命周期管理
├── requirements.txt
├── Dockerfile
├── entrypoint.sh
├── config/
│   ├── preference.yaml        # 用户偏好 + 风控规则
│   └── schema.py              # Pydantic 配置校验
├── layers/
│   ├── __init__.py
│   ├── macro.py               # ① 宏观叙事层
│   ├── sector.py              # ② 板块轮动层
│   ├── preference.py          # ③ 偏好过滤层
│   └── screening.py           # ④ 候选池筛选层
├── pipeline.py                # 四层编排器
├── scheduler.py               # APScheduler 定时任务
├── models.py                  # Pydantic 数据模型
└── vendors/
    └── vibe_trading.py        # Vibe-Trading 适配层
```

### 2.2 与现有服务的关系

```
                    ┌─────────────────┐
                    │ Vibe-Trading    │
                    │ (agent/loaders) │
                    └────────┬────────┘
                             │ 库级 import
                    ┌────────▼────────┐
                    │ screener-bridge │  ← 新增
                    │   (FastAPI)     │
                    └────────┬────────┘
                             │ HTTP: 候选池结果
                    ┌────────▼────────┐
                    │ trading-bridge  │  ← 现有
                    │   (FastAPI)     │
                    └────────┬────────┘
                             │ HTTP: 交易信号
                    ┌────────▼────────┐
                    │ executor-bridge │  ← 现有
                    │   (FastAPI)     │
                    └─────────────────┘
```

### 2.3 管线数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                    APScheduler (8:30 AM A股盘前)                   │
│                                                                   │
│  ① macro.py                                                      │
│  ┌──────────────────────────────────────────────────────┐        │
│  │ 数据源: Anthropic API + yfinance + AKShare            │        │
│  │ 输出: MacroRegime { regime, confidence, reason,       │        │
│  │         suggested_exposure }                          │        │
│  │ 缓存: Redis, TTL=至下一交易日盘前                      │        │
│  └────────────┬─────────────────────────────────────────┘        │
│               │ confidence<0.6 → 降半仓; risk_off → 防御板块      │
│               ▼                                                   │
│  ② sector.py                                                     │
│  ┌──────────────────────────────────────────────────────┐        │
│  │ 数据源: AKShare(北向/南向) + yfinance(Sector ETF)     │        │
│  │ 评分: 5日超额收益×0.4 + 资金流入排名×0.4             │        │
│  │       + 成交量放量比×0.2                             │        │
│  │ 输出: SectorRanking { top_sectors[], scores[] }       │        │
│  │ 缓存: Redis, TTL=1 day                                │        │
│  └──────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│             POST /screen (按需或 trading-bridge 调用)             │
│                                                                   │
│  ③ preference.py                                                 │
│  ┌──────────────────────────────────────────────────────┐        │
│  │ 输入: 板块候选 + preference.yaml                      │        │
│  │ 处理: 排除黑名单板块 → 排除小票 → 板块集中度截断     │        │
│  │ 输出: FilteredCandidates { stocks[], excluded[] }     │        │
│  └────────────┬─────────────────────────────────────────┘        │
│               ▼                                                   │
│  ④ screening.py                                                  │
│  ┌──────────────────────────────────────────────────────┐        │
│  │ 筛选条件 (pandas 向量化批量扫):                       │        │
│  │  - turnover_rate > 1.5%    流动性                     │        │
│  │  - close > ma20            趋势向上                   │        │
│  │  - volume_ratio > 1.2      量能放大                   │        │
│  │  - 5 < pe_ttm < 60         估值合理                   │        │
│  │  - market_cap > 5e9 CNY    市值门槛                   │        │
│  │ 输出: ScreenedPool { candidates[10~30], metadata }    │        │
│  └──────────────────────────────────────────────────────┘        │
│                              │                                    │
│                              ▼                                    │
│              返回 API 响应 + 写入 data/screening_results.json     │
└─────────────────────────────────────────────────────────────────┘
```

## 3. 数据模型

### 3.1 核心结构

```python
class MarketRegime(str, Enum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"

class Market(str, Enum):
    CN = "cn"
    HK = "hk"
    US = "us"

class MacroRegime(BaseModel):
    regime: MarketRegime
    confidence: float          # 0.0 ~ 1.0
    reason: str                # LLM 推理摘要
    indicators: dict           # {vix_spread, csi300_vs_ma250, sentiment_idx}
    suggested_exposure: float  # 0.0 ~ 1.0, 建议仓位
    stale: bool = False        # True 表示来自过期缓存（降级运行）
    generated_at: datetime

class SectorScore(BaseModel):
    sector: str
    market: Market
    score: float               # 综合评分
    excess_return_5d: float
    fund_flow_rank: int
    volume_ratio: float

class SectorRanking(BaseModel):
    top_sectors: list[SectorScore]   # 3~5 个
    all_scores: list[SectorScore]
    generated_at: datetime
    regime_context: MacroRegime | None

class ScreenedStock(BaseModel):
    symbol: str                # SH600519
    name: str
    market: Market
    sector: str
    score: float               # 综合因子得分
    turnover_rate: float
    pe_ttm: float
    volume_ratio: float
    market_cap: float
    filters_passed: list[str]

class ScreeningResult(BaseModel):
    regime: MacroRegime | None
    top_sectors: list[SectorScore]
    candidates: list[ScreenedStock]
    excluded_count: int
    total_scanned: int
    elapsed_ms: int
    generated_at: datetime
```

### 3.2 管线中间状态 (Redis)

```
Key                         Type    TTL       描述
─────────────────────────────────────────────────────────
screeener:macro:latest      hash    至次日盘前  最新宏观判断
screeener:sectors:cn:latest hash    1 day       A股板块排名
screeener:sectors:hk:latest hash    1 day       港股板块排名
screeener:sectors:us:latest hash    1 day       美股板块排名
screeener:last-run           hash    7 days     最近一次全管线运行元数据
```

## 4. API 设计

| Method | Path | 描述 |
|--------|------|------|
| `GET` | `/health` | 服务健康 + 缓存状态 |
| `POST` | `/screen` | 触发完整四层管线 |
| `GET` | `/macro` | 最新宏观判断（优先缓存） |
| `GET` | `/sectors` | 最新板块排名 `?market=cn` |
| `POST` | `/scheduler/run-macro` | 手动触发宏观层 |
| `POST` | `/scheduler/run-sector` | 手动触发板块层 |
| `GET` | `/preferences` | 读取当前偏好配置 |
| `PUT` | `/preferences` | 更新偏好配置 |

### `POST /screen` 请求/响应示例

**请求:**
```json
{
    "markets": ["cn", "hk"],
    "sectors": ["半导体", "新能源"],
    "start_from": "macro",
    "top_n": 20
}
```
- `markets`: 可选，默认全部
- `sectors`: 可选，不传则使用 sector 层输出的 Top N
- `start_from`: 可选，`macro` | `sector` | `preference` | `screening`
- `top_n`: 可选，默认 20

**响应 (200):**
```json
{
    "regime": {
        "regime": "risk_on",
        "confidence": 0.78,
        "reason": "PMI连续3月扩张，北向资金持续流入...",
        "suggested_exposure": 0.8,
        "generated_at": "2026-05-12T08:35:00Z"
    },
    "top_sectors": [
        {"sector": "半导体", "market": "cn", "score": 0.87, ...},
        {"sector": "新能源", "market": "cn", "score": 0.79, ...}
    ],
    "candidates": [
        {
            "symbol": "SH688981",
            "name": "中芯国际",
            "market": "cn",
            "sector": "半导体",
            "score": 0.92,
            "turnover_rate": 3.2,
            "pe_ttm": 45.0,
            "volume_ratio": 1.8,
            "market_cap": 4.2e11,
            "filters_passed": ["turnover", "trend", "volume", "pe", "cap"]
        }
    ],
    "excluded_count": 12,
    "total_scanned": 5230,
    "elapsed_ms": 3400,
    "generated_at": "2026-05-12T09:00:00Z"
}
```

### 错误响应 (统一格式)
```json
{
    "error": "macro_layer_failed",
    "detail": "LLM API timeout after 30s",
    "partial_results": { ... }
}
```
- `partial_results`: 返回已完成层的缓存结果，允许降级运行

## 5. 各层实现

### 5.1 宏观叙事层 (`layers/macro.py`)

**数据源:**
| 指标 | 数据源 | 用途 |
|------|--------|------|
| FED声明/财经新闻/CPI/PMI/非农 | Anthropic API (Claude) | LLM 推理输出 regime + reason |
| VIX 均线偏离 | yfinance (`^VIX`) | 量化补充：VIX vs MA20 |
| 沪深300 vs MA250 | AKShare / yfinance | A股牛熊分界 |
| A股情绪指数 | AKShare | 散户情绪参考 |

**实现要点:**
- 使用 `anthropic` SDK 直接调用，非 LangChain 包装
- 缓存到 Redis，TTL=至下一交易日盘前
- confidence < 0.6 时自动降级 (`suggested_exposure *= 0.5`)
- 超时 30s，失败时使用上次缓存 + 标记 stale

**LLM Prompt 模板 (精简):**
```
你是宏观策略分析师。基于以下数据判断市场环境：

宏观经济数据:
- CPI: {cpi}
- PMI: {pmi}
- 非农: {nonfarm}
- FED最近的声明摘要: {fed_statement}

量化指标:
- VIX 当前: {vix} (MA20: {vix_ma20})
- 沪深300 当前: {csi300} (MA250: {csi300_ma250})

请输出 JSON:
{"regime": "risk_on|neutral|risk_off", "confidence": 0.0~1.0, "reason": "..."}
```

### 5.2 板块轮动层 (`layers/sector.py`)

**数据源 (按市场):**
| 市场 | 数据 | 来源 |
|------|------|------|
| A股 | 北向资金净流入、申万行业指数 | AKShare / Vibe-Trading loader |
| 港股 | 南向资金、恒生行业板块 | AKShare / futu-api |
| 美股 | Sector ETF (XLK/XLE/XLP...) vs SPY | yfinance |

**评分公式:**
```
score = excess_return_5d × 0.4 + fund_flow_rank_norm × 0.4 + volume_expansion × 0.2
```
- `excess_return_5d`: 板块相对大盘 5 日超额收益
- `fund_flow_rank_norm`: 资金净流入排名归一化 (0~1)
- `volume_expansion`: 近5日均量 / 近20日均量

**宏观联动:**
- `risk_on` → 偏好周期/成长板块
- `risk_off` → 偏好防御板块（公用事业、消费必需品）
- `neutral` → 无偏好调整

### 5.3 偏好过滤层 (`layers/preference.py`)

**静态配置 (`config/preference.yaml`):**
```yaml
risk:
  max_single_position: 0.08        # 单票最大仓位
  max_sector_concentration: 0.3    # 单板块最大集中度
  min_market_cap_cny: 5e9          # 最小市值 (50亿)

blacklist_sectors:
  - "博彩"
  - "军工"

preferred_holding_days:
  min: 5
  max: 20

markets:
  cn: true
  hk: true
  us: true
```

**实现要点:**
- Python `dataclass` + Pydantic 校验，启动时加载
- 纯规则引擎，无 ML
- `PUT /preferences` 支持运行时修改（写入 YAML + 内存热更新）
- 排除原因写入结果 (`excluded_reasons`)

### 5.4 候选池筛选层 (`layers/screening.py`)

**筛选条件 (可配置):**

| 条件 | 默认阈值 | 目的 |
|------|----------|------|
| `turnover_rate > 1.5%` | 1.5% | 排除仙股 |
| `close > ma20` | — | 趋势过滤 |
| `volume_ratio > 1.2` | 1.2 | 量能确认 |
| `5 < pe_ttm < 60` | 5~60 | 估值过滤 |
| `market_cap > min_market_cap` | 5e9 CNY | 市值门槛 |

**复用 Vibe-Trading (库级):**
```python
# vendors/vibe_trading.py
from agent.backtest.loaders.akshare_loader import AKShareLoader
from agent.backtest.loaders.yfinance_loader import YFinanceLoader

def fetch_cn_market_snapshot(symbols: list[str]) -> pd.DataFrame:
    loader = AKShareLoader()
    return loader.fetch_daily(symbols, start_date, end_date)

def fetch_hk_us_snapshot(symbols: list[str]) -> pd.DataFrame:
    loader = YFinanceLoader()
    return loader.fetch_daily(symbols, start_date, end_date)
```

**股票池来源:**
- A股: AKShare `stock_info_a_code_name()` 全量 → 剔除 ST/*ST/退市 → 按板块过滤
- 港股: AKShare `stock_hk_spot()` 全量 → 按板块过滤
- 美股: yfinance 按 Sector ETF 成分股获取 → 按板块过滤

**实现要点:**
- pandas 向量化过滤，一次扫完全市场（几秒完成）
- 筛选条件配置化（可放 preference.yaml 扩展）
- 输出排序: 等权多因子打分（流通市值中性化后的 Z-score 均值）

## 6. 定时调度

### 6.1 调度配置 (`scheduler.py`)

使用 APScheduler，在 FastAPI 生命周期中管理：

| 任务 | Cron | 描述 |
|------|------|------|
| `run_macro` | `30 8 * * 1-5` | 交易日盘前 8:30 |
| `run_sector` | `35 8 * * 1-5` | 宏观完成后 5 分钟 |

- `run_sector` 依赖 `run_macro` 成功（或使用过期缓存）
- 提供手动触发端点用于测试
- 支持交易日历过滤（可选，初版用周一到周五）

### 6.2 调度器生命周期

```python
# scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

def init_scheduler():
    scheduler.add_job(run_macro,  "cron", day_of_week="mon-fri", hour=8, minute=30, id="macro")
    scheduler.add_job(run_sector, "cron", day_of_week="mon-fri", hour=8, minute=35, id="sector")
    scheduler.start()

def shutdown_scheduler():
    scheduler.shutdown()
```

## 7. 容错 & 降级

| 场景 | 策略 |
|------|------|
| LLM API 超时/不可用 | 使用上一次缓存 + 标记 `stale: true` |
| AKShare 数据源不可用 | 回退到 Vibe-Trading 的 loader fallback chain |
| 板块层失败 | 跳过板块聚焦，全市场筛选 |
| 宏观 confidence < 0.6 | 自动降半仓，管线继续 |
| Redis 不可用 | 降级为实时计算，不缓存 |
| 某市场数据缺失 | 跳过该市场，返回 `skipped_markets: ["us"]` |

## 8. Docker 集成

```yaml
# docker/docker-compose.yml 新增
screener-bridge:
  build:
    context: ../tools/screener-bridge
    dockerfile: Dockerfile
  container_name: screener-bridge
  restart: unless-stopped
  init: true
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true
  env_file:
    - ../.env
  environment:
    - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    - REDIS_HOST=redis
    - REDIS_PASSWORD=${REDIS_PASSWORD}
    - VIBE_TRADING_PATH=/app/Vibe-Trading
  ports:
    - "8002:8002"
  volumes:
    - ${VIBE_TRADING_HOST_PATH:-/mnt/d/Project/Vibe-Trading}:/app/Vibe-Trading:ro
  depends_on:
    redis:
      condition: service_healthy
```

## 9. 关键依赖

```
# requirements.txt
fastapi>=0.110
uvicorn[standard]
pydantic>=2.0
httpx
redis[hiredis]
pyyaml
apscheduler
pandas>=2.0
numpy
akshare
yfinance
anthropic
```

Vibe-Trading 通过卷挂载 + `sys.path` 注入，不 pip install。

## 10. 测试策略

| 层级 | 覆盖内容 | 方式 |
|------|----------|------|
| 单元测试 | preference 规则引擎、screening 过滤逻辑、评分计算 | pytest + mock 数据 |
| 集成测试 | 各层 API 端点、Redis 缓存、调度器触发 | pytest + httpx + fakeredis |
| 端到端 | `POST /screen` 全管线 (mock 外部 API) | pytest + vcrpy |
| 手动验证 | 真实数据跑一次全管线，检查输出合理性 | `analyze_holdings.py` 风格脚本 |

## 11. 与 trading-bridge 对接

trading-bridge 新增可选流程：

```
POST /analyze 请求新增字段:
{
    "ticker": "SH600519",
    "date": "2026-05-12",
    "screening_context": { ... }   // 可选，来自 screener-bridge 的筛选上下文
}
```

或者在 analyze_holdings.py 中：
```python
# 1. 调用 screener-bridge POST /screen 获取候选池
# 2. 仅对候选池中的持仓运行 TradingAgents-CN 分析
# 3. 池外持仓直接标记 "不在当前筛选中"
```

## 12. 实施路线图

| Phase | 内容 | 预计工作量 |
|-------|------|-----------|
| Phase 1 | 项目骨架 — FastAPI + Docker + Redis + scheduler 框架 | ~0.5 天 |
| Phase 2 | 偏好过滤层 (最简单，无外部依赖) | ~0.5 天 |
| Phase 3 | 候选池筛选层 (Vibe-Trading loader 集成) | ~1 天 |
| Phase 4 | 板块轮动层 (多市场数据) | ~1 天 |
| Phase 5 | 宏观叙事层 (LLM + 量化指标) | ~1 天 |
| Phase 6 | 管线编排 + 容错 + 对接 trading-bridge | ~1 天 |
| Phase 7 | 测试 + 文档 + Docker 集成 | ~0.5 天 |

**总计: ~5.5 天**
