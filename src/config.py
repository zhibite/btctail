"""Global configuration.

Priority (low -> high):
    code defaults < config.toml < .env.docker < .env < process env
    < runtime overrides stored in the database (see src/web/runtime_config.py)

Secrets (API keys, private key, dashboard password) belong in .env or the
deployment platform's environment variables -- never in config.toml.
"""
from __future__ import annotations

import math
import os
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent

# Load .env files into os.environ *before* Settings is built, so that the
# precedence above is honoured uniformly (pydantic-settings alone lets init
# kwargs beat env-file values, which would make config.toml outrank .env).
# override=False keeps real process env variables on top.
for _name in (".env", ".env.docker"):
    _p = _ROOT / _name
    if _p.exists():
        load_dotenv(_p, override=False)


@lru_cache(maxsize=1)
def toml_data() -> dict:
    path = _ROOT / "config.toml"
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return {k.lower(): v for k, v in tomllib.load(f).items()}


def toml_section(name: str) -> dict:
    """Section values as uppercase keys, without overriding real env vars."""
    section = toml_data().get(name.lower(), {}) or {}
    out: dict = {}
    for key, value in section.items():
        upper = key.upper()
        if os.getenv(upper) is None:
            out[upper] = value
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,          # .env files are loaded above, deliberately
        extra="ignore",
        case_sensitive=True,
    )

    # ── Identity ──────────────────────────────────────────────────────────
    APP_NAME: str = "PolyTail BTC"
    ASSET: str = "BTC"
    PERIOD: str = "15m"
    ROUND_PERIOD_S: int = 900

    # paper = simulate fills, record everything, never touch the wallet
    # live  = real orders on Polymarket (GTD maker first, FAK fallback)
    # These are the defaults; the dashboard can switch per strategy at runtime.
    TRADING_MODE: str = "live"

    # ── Strategy (the parameters from docs/late_window_strategy_summary.md) ──
    # The generic names configure the **15m** strategy (the documented,
    # frozen configuration). The 5m strategy has its own *_5M overrides and
    # is the one meant for continuous agent tuning.
    ENTRY_WINDOW_SEC: float = 60.0     # only act in the last N seconds
    ENTRY_THRESHOLD: float = 0.88      # first side to reach this triggers
    CONFIRM_SEC: float = 10.0          # wait, then require it is still >= threshold
    MAX_ENTRY_PRICE: float = 0.94      # skip if the confirmed price is >= this
    MIN_ENTRY_PRICE: float = 0.0       # 0 disables
    STAKE_USD: float = 10.0          # backtest notional (same as live notional)
    # Live sizing is by USDC. A GTD limit order must be sized in shares, so
    # the runtime converts: shares = notional / limit (rounded down), which
    # keeps the documented 10 USDC per trade and the 5-share CLOB minimum.
    ENTRY_NOTIONAL_USD: float = 10.0
    # "gtd": rest a maker limit at the signal price, then optionally fall
    # back to FAK if it has not filled. "fak": send a taker order directly.
    ENTRY_ORDER_TYPE: str = "gtd"
    GTD_FILL_TIMEOUT_S: float = 3.0
    GTD_POST_ONLY: bool = True
    ENTRY_FALLBACK_FAK: bool = True
    MAKER_FEE_RATE: float = 0.0      # crypto maker fee is 0
    REARM_AFTER_FAILED_CONFIRM: bool = False
    MAX_ENTRIES_PER_WINDOW: int = 1

    # ── Strategies to run (one process, one loop per strategy) ────────────
    ENABLED_15M: bool = True
    ENABLED_5M: bool = True
    # Per-strategy mode. Empty = inherit TRADING_MODE. Defaults follow the
    # operator's decision: 15m live, 5m paper until the agent has found
    # something worth trading.
    TRADING_MODE_5M: str = "paper"

    # 5m defaults start from the doc's best simple configuration
    # (0.90 / no confirmation / 0.92 cap) and use the last 20 seconds, which
    # the June-October experiment found superior to 30s: +404.17U / ROI
    # +1.35% / t=2.67 vs the 30s baseline +308.68 / +0.69% / t=1.61, and it
    # survives 0.01 slippage (+248.60) while the baseline barely does
    # (+78.29). The confirm filter stays off because the window is too short.
    ENTRY_WINDOW_SEC_5M: float = 20.0
    ENTRY_THRESHOLD_5M: float = 0.90
    CONFIRM_SEC_5M: float = 0.0
    MAX_ENTRY_PRICE_5M: float = 0.92
    MIN_ENTRY_PRICE_5M: float = 0.0
    # Backtest stake and live order size share one number so the research
    # results and the deployed strategy cannot drift apart. The operator
    # chose 10 USDC for 5m after the rare-entry review.
    STAKE_USD_5M: float = 10.0
    ENTRY_NOTIONAL_USD_5M: float = 10.0
    REARM_AFTER_FAILED_CONFIRM_5M: bool = False

    # ── Execution ─────────────────────────────────────────────────────────
    # The signal price is the CLOB midpoint (same series the backtest used).
    # The FAK limit is signal_price + EXEC_SLIPPAGE, clamped to EXEC_LIMIT_MAX.
    SIGNAL_PRICE_SOURCE: str = "mid"   # mid | last | ask
    EXEC_SLIPPAGE: float = 0.01
    EXEC_LIMIT_MAX: float = 0.99
    MAX_SPREAD_AT_ENTRY: float = 0.06  # 0 disables
    MIN_BOOK_DEPTH_USD: float = 0.0    # 0 disables; checks depth at/below limit
    TAKER_FEE_RATE: float = 0.07       # fee = shares * rate * p * (1-p)
    # Prefer the market's own feeRate (Gamma field) when present. The CLOB
    # /fee-rate endpoint is only consulted when this is on: its `base_fee`
    # unit has been observed both as bps and as the raw rate, so leaving it
    # off keeps accounting pinned to the documented 0.07 until an operator
    # has verified the value for a market.
    FEE_RATE_AUTODETECT: bool = False
    USE_MARKET_WS: bool = True
    POLL_INTERVAL_MS: int = 1000       # outside the entry window
    ENTRY_POLL_INTERVAL_MS: int = 250  # inside the entry window
    # Dashboard-only polling before the collection/entry window, so the
    # panel shows live prices for the whole window instead of "--".
    DISPLAY_POLL_INTERVAL_MS: int = 5000

    # ── Risk guards ───────────────────────────────────────────────────────
    DAILY_LOSS_LIMIT_USD: float = 50.0     # 0 disables
    MAX_CONSEC_LOSSES: int = 3             # 0 disables
    PAUSE_MINUTES_AFTER_STREAK: int = 60
    MIN_WALLET_BALANCE_USD: float = 15.0
    RISK_RESERVE_USD: float = 2.0
    SETTLE_TIMEOUT_S: int = 3600           # unresolved after this -> flag
    RESOLUTION_POLL_S: int = 15

    # ── Paper trading ─────────────────────────────────────────────────────
    PAPER_START_BALANCE: float = 1000.0
    PAPER_SLIPPAGE: float = 0.005      # used when no order book is available
    PAPER_USE_BOOK: bool = True

    # ── Data collection (feeds the tuning agent) ──────────────────────────
    DATA_COLLECT_ENABLED: bool = True
    COLLECT_SEC: int = 300             # sample the last N seconds of each window
    DATA_DIR: str = ""                 # default <repo>/data

    # ── CLOB credentials / endpoints ──────────────────────────────────────
    API_KEY: str = ""
    API_SECRET: str = ""
    API_PASSPHRASE: str = ""
    WALLET_PRIVATE_KEY: str = ""
    SIGNATURE_TYPE: int = 0            # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE
    FUNDER_ADDRESS: str = ""
    GAMMA_API: str = "https://gamma-api.polymarket.com"
    POLYMARKET_HOST: str = "https://clob.polymarket.com"

    # ── Web dashboard ─────────────────────────────────────────────────────
    WEB_ENABLED: bool = True
    WEB_HOST: str = "0.0.0.0"
    WEB_PORT: int = 8013
    WEB_LOG_LEVEL: str = "warning"
    WEB_USERNAME: str = "admin"
    WEB_PASSWORD: str = ""
    WEB_SESSION_TTL_S: int = 7 * 24 * 3600

    # ── Research / agent ──────────────────────────────────────────────────
    DATASET_ROOT: str = ""             # default: ../predicttick/data/dataset if present
    RESEARCH_OUTPUT_DIR: str = ""      # default <data>/research
    AGENT_MODEL: str = "claude-opus-5-5"
    AGENT_MAX_ITERATIONS: int = 8
    AGENT_MAX_TOKENS: int = 16000
    AGENT_MAX_BACKTESTS: int = 40
    ANTHROPIC_API_KEY: str = ""

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    HEARTBEAT_S: int = 30

    # ── Derived helpers ───────────────────────────────────────────────────

    @property
    def asset_lower(self) -> str:
        return self.ASSET.strip().lower()

    @property
    def slug_prefix(self) -> str:
        return f"{self.asset_lower}-updown-{self.PERIOD}"

    @property
    def window_seconds(self) -> int:
        return 300 if self.PERIOD == "5m" else 900

    @property
    def data_dir(self) -> Path:
        return Path(self.DATA_DIR) if self.DATA_DIR else _ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "polytail.db"

    @property
    def collect_dir(self) -> Path:
        return self.data_dir / "dataset"

    @property
    def research_dir(self) -> Path:
        if self.RESEARCH_OUTPUT_DIR:
            return Path(self.RESEARCH_OUTPUT_DIR)
        return self.data_dir / "research"

    @property
    def dataset_root(self) -> Path:
        if self.DATASET_ROOT:
            return Path(self.DATASET_ROOT)
        # Prefer the copy shipped/downloaded into this project, then the
        # sibling research export used during development.
        local = self.collect_dir
        if local.exists() and any(local.glob("asset=*/period=*/date=*")):
            return local
        sibling = _ROOT.parent.parent / "predicttick" / "data" / "dataset"
        return sibling if sibling.exists() else local

    @property
    def is_paper(self) -> bool:
        return self.TRADING_MODE.strip().lower() != "live"

    @property
    def is_live(self) -> bool:
        return not self.is_paper

    @property
    def has_credentials(self) -> bool:
        return bool(self.API_KEY and self.API_SECRET and self.API_PASSPHRASE)

    def entry_limit_for(self, signal_price: float) -> float:
        """Marketable limit for a taker BUY at `signal_price`."""
        return min(self.EXEC_LIMIT_MAX, round(signal_price + self.EXEC_SLIPPAGE, 4))

    def strategy_params(self) -> dict:
        """The subset the strategy engine consumes (for this settings' PERIOD)."""
        return {
            "entry_window_sec": float(self.ENTRY_WINDOW_SEC),
            "threshold": float(self.ENTRY_THRESHOLD),
            "confirm_sec": float(self.CONFIRM_SEC),
            "max_entry_price": float(self.MAX_ENTRY_PRICE),
            "min_entry_price": float(self.MIN_ENTRY_PRICE),
            "rearm_after_failed_confirm": bool(self.REARM_AFTER_FAILED_CONFIRM),
        }

    # ── Multi-strategy helpers ────────────────────────────────────────────

    def for_period(self, period: str) -> "Settings":
        """Independent settings for one strategy.

        Everything (broker mode, stake, strategy params) is per strategy, so
        the 15m and 5m runtimes can share a process and a database without
        touching each other's configuration. Runtime overrides are applied to
        the returned clone by ``src/web/runtime_config``.
        """
        clone = self.model_copy(deep=True)
        clone.PERIOD = period
        if period == "5m":
            clone.ENTRY_WINDOW_SEC = float(self.ENTRY_WINDOW_SEC_5M)
            clone.ENTRY_THRESHOLD = float(self.ENTRY_THRESHOLD_5M)
            clone.CONFIRM_SEC = float(self.CONFIRM_SEC_5M)
            clone.MAX_ENTRY_PRICE = float(self.MAX_ENTRY_PRICE_5M)
            clone.MIN_ENTRY_PRICE = float(self.MIN_ENTRY_PRICE_5M)
            clone.STAKE_USD = float(self.STAKE_USD_5M)
            clone.ENTRY_NOTIONAL_USD = float(self.ENTRY_NOTIONAL_USD_5M)
            clone.REARM_AFTER_FAILED_CONFIRM = bool(self.REARM_AFTER_FAILED_CONFIRM_5M)
            clone.TRADING_MODE = (self.TRADING_MODE_5M or self.TRADING_MODE).strip().lower()
        return clone

    def enabled_periods(self) -> list[str]:
        periods = []
        if self.ENABLED_15M:
            periods.append("15m")
        if self.ENABLED_5M:
            periods.append("5m")
        return periods

    def strategy_settings(self) -> dict[str, "Settings"]:
        """{period: per-strategy Settings} for everything enabled."""
        return {period: self.for_period(period) for period in self.enabled_periods()}

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        for name in ("ENTRY_WINDOW_SEC", "ENTRY_THRESHOLD", "CONFIRM_SEC",
                     "MAX_ENTRY_PRICE", "MIN_ENTRY_PRICE", "STAKE_USD",
                     "ENTRY_NOTIONAL_USD", "EXEC_SLIPPAGE", "TAKER_FEE_RATE"):
            try:
                if not math.isfinite(float(getattr(self, name))):
                    errors.append(f"{name} 必须是有限数字")
            except (TypeError, ValueError):
                errors.append(f"{name} 必须是数字")
        if errors:
            return errors
        if self.PERIOD not in ("15m", "5m"):
            errors.append("PERIOD 必须是 15m 或 5m")
        if not 0.0 < self.ENTRY_THRESHOLD < 1.0:
            errors.append("ENTRY_THRESHOLD 必须在 (0, 1) 内")
        if not 0.0 < self.MAX_ENTRY_PRICE < 1.0:
            errors.append("MAX_ENTRY_PRICE 必须在 (0, 1) 内")
        if self.MAX_ENTRY_PRICE < self.ENTRY_THRESHOLD:
            errors.append("MAX_ENTRY_PRICE 不能低于 ENTRY_THRESHOLD")
        if self.MIN_ENTRY_PRICE >= self.MAX_ENTRY_PRICE:
            errors.append("MIN_ENTRY_PRICE 必须低于 MAX_ENTRY_PRICE")
        if self.ENTRY_WINDOW_SEC <= 0 or self.ENTRY_WINDOW_SEC > self.window_seconds:
            errors.append("ENTRY_WINDOW_SEC 必须在 (0, 窗口长度] 内")
        if self.CONFIRM_SEC < 0 or self.CONFIRM_SEC >= self.ENTRY_WINDOW_SEC:
            errors.append("CONFIRM_SEC 必须 < ENTRY_WINDOW_SEC")
        if self.STAKE_USD <= 0:
            errors.append("STAKE_USD 必须 > 0")
        if self.EXEC_SLIPPAGE < 0:
            errors.append("EXEC_SLIPPAGE 不能为负")
        if self.TAKER_FEE_RATE < 0 or self.TAKER_FEE_RATE > 0.5:
            errors.append("TAKER_FEE_RATE 必须在 [0, 0.5] 内")
        if self.TRADING_MODE not in ("paper", "live"):
            errors.append("TRADING_MODE 必须是 paper 或 live")
        if self.ENTRY_ORDER_TYPE not in ("gtd", "fak"):
            errors.append("ENTRY_ORDER_TYPE 必须是 gtd 或 fak")
        if self.ENTRY_NOTIONAL_USD <= 0:
            errors.append("ENTRY_NOTIONAL_USD 必须 > 0")
        # A GTD is sized in shares; at the highest allowed price the order
        # must still reach the CLOB's 5-share minimum.
        if (
            self.ENTRY_ORDER_TYPE == "gtd"
            and self.MAX_ENTRY_PRICE > 0
            and self.ENTRY_NOTIONAL_USD / self.MAX_ENTRY_PRICE < 5
        ):
            errors.append(
                "GTD 挂单最少 5 股；请把单笔金额提高到至少 5 × 入场价上限，或改用 fak"
            )
        if self.GTD_FILL_TIMEOUT_S <= 0:
            errors.append("GTD_FILL_TIMEOUT_S 必须 > 0")
        if self.MAKER_FEE_RATE < 0 or self.MAKER_FEE_RATE > 0.5:
            errors.append("MAKER_FEE_RATE 必须在 [0, 0.5] 内")
        return errors


# Fields the dashboard / agent may change at runtime, with bounds used by
# both server-side validation and the research optimizer.
TUNABLE_SPECS: dict[str, dict] = {
    "ENTRY_WINDOW_SEC": {"group": "strategy", "type": "int", "min": 10, "max": 600, "step": 5,
                         "label": "入场窗口(秒)",
                         "help": "只看窗口最后 N 秒。回测推荐 60。"},
    "ENTRY_THRESHOLD": {"group": "strategy", "type": "float", "min": 0.80, "max": 0.99, "step": 0.01,
                        "label": "触发阈值",
                        "help": "任一侧价格首次达到该值即进入确认。推荐 0.90。"},
    "CONFIRM_SEC": {"group": "strategy", "type": "float", "min": 0, "max": 120, "step": 1,
                    "label": "确认秒数",
                    "help": "触发后等待 N 秒，仍在阈值上方才买入。推荐 10。"},
    "MAX_ENTRY_PRICE": {"group": "strategy", "type": "float", "min": 0.85, "max": 0.99, "step": 0.01,
                        "label": "入场价上限",
                        "help": "确认价 >= 该值则放弃。推荐 0.94（0.94-0.95 档价差偏弱）。"},
    "MIN_ENTRY_PRICE": {"group": "strategy", "type": "float", "min": 0.0, "max": 0.95, "step": 0.01,
                        "label": "入场价下限",
                        "help": "0 表示不启用。"},
    "ENTRY_NOTIONAL_USD": {"group": "strategy", "type": "float", "min": 1, "max": 100000,
                           "step": 1, "label": "单笔金额(USDC)",
                           "help": "按金额下单；GTD 会按限价换算成股数（最少 5 股）。"},
    "EXEC_SLIPPAGE": {"group": "execution", "type": "float", "min": 0.0, "max": 0.05, "step": 0.001,
                      "label": "执行滑点上限",
                      "help": "GTD/FAK 限价 = 信号价 + 该值。回测敏感性 0.005 / 0.01。"},
    "ENTRY_ORDER_TYPE": {"group": "execution", "type": "str", "options": ["gtd", "fak"],
                         "label": "下单方式",
                         "help": "gtd = 先挂限价卖单（maker，费率 0）；fak = 直接吃单。"},
    "GTD_FILL_TIMEOUT_S": {"group": "execution", "type": "float", "min": 0.5, "max": 30, "step": 0.5,
                           "label": "GTD 等待成交(秒)",
                           "help": "挂单后最多等这么久；仍未成交则按需回退 FAK 或放弃。"},
    "GTD_POST_ONLY": {"group": "execution", "type": "bool",
                      "label": "GTD 只做 maker",
                      "help": "开启后限价单不会主动吃单；若会成交则被拒绝并回退。"},
    "ENTRY_FALLBACK_FAK": {"group": "execution", "type": "bool",
                           "label": "GTD 未成交时回退 FAK",
                           "help": "关闭则 GTD 没成交就放弃本窗口。"},
    "MAKER_FEE_RATE": {"group": "execution", "type": "float", "min": 0.0, "max": 0.2, "step": 0.001,
                       "label": "Maker 费率",
                       "help": "Crypto 类目 maker 费率通常为 0。"},
    "MAX_SPREAD_AT_ENTRY": {"group": "execution", "type": "float", "min": 0.0, "max": 0.3, "step": 0.005,
                            "label": "最大买卖价差",
                            "help": "入场时 (ask-bid) 超过该值则放弃。0=关闭。"},
    "TAKER_FEE_RATE": {"group": "execution", "type": "float", "min": 0.0, "max": 0.2, "step": 0.001,
                       "label": "官方费率",
                       "help": "fee = 份额 × 费率 × p × (1-p)。Crypto 类目默认 0.07。"},
    "DAILY_LOSS_LIMIT_USD": {"group": "risk", "type": "float", "min": 0, "max": 10000, "step": 5,
                             "label": "单日亏损上限(USDC)",
                             "help": "当日已实现亏损达到该值即停止当日交易。0=关闭。"},
    "MAX_CONSEC_LOSSES": {"group": "risk", "type": "int", "min": 0, "max": 20, "step": 1,
                          "label": "最大连亏次数",
                          "help": "连续亏损达到该值后暂停。0=关闭。"},
    "PAUSE_MINUTES_AFTER_STREAK": {"group": "risk", "type": "int", "min": 1, "max": 1440, "step": 5,
                                   "label": "连亏暂停(分钟)", "help": "连亏触发后的暂停时长。"},
    "MIN_WALLET_BALANCE_USD": {"group": "risk", "type": "float", "min": 0, "max": 100000, "step": 1,
                               "label": "最低钱包余额",
                               "help": "低于该值不再开新仓。"},
}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_settings: Settings | None = None


def _toml_init() -> dict:
    """config.toml sections as Settings init kwargs (env vars already win)."""
    merged: dict = {}
    for section in ("GENERAL", "BINANCE", "LOG"):
        merged.update(toml_section(section))
    return merged


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings(**_toml_init())
    return _settings


def build() -> Settings:
    """A fresh Settings from config.toml + environment, without touching the
    singleton the running bot holds."""
    return Settings(**_toml_init())


def reload() -> Settings:
    """Replace the singleton (used by tests / explicit reloads)."""
    global _settings
    _settings = Settings(**_toml_init())
    return _settings

