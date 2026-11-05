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

# ---------------------------------------------------------------------------
# Multi-asset tail strategies
# ---------------------------------------------------------------------------
# The seven crypto Up/Down assets listed by Polymarket for the 5m / 15m
# series. The order here is the order shown on the dashboard / config page.
ASSET_ORDER: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
PERIODS: tuple[str, ...] = ("15m", "5m")

# Default per-strategy parameters from docs/*_optimal_parameters.md
# (the "稳健版" table, 10 USDC per trade). Format:
#   entry_window_sec / threshold / confirm_sec / max_entry_price
#
# XRP has no dedicated research report in docs/, so it starts from the
# conservative generic configuration used by BTC before the 2026 tuning
# (60s / 0.90 / 10s / 0.94 and 20s / 0.90 / 0s / 0.92). Everything ships in
# paper mode until an operator deliberately switches a strategy to live.
RECOMMENDED_PARAMS: dict[tuple[str, str], dict[str, float]] = {
    ("BTC", "15m"): {"entry_window_sec": 60, "threshold": 0.94,
                     "confirm_sec": 15, "max_entry_price": 0.96},
    ("BTC", "5m"): {"entry_window_sec": 10, "threshold": 0.88,
                    "confirm_sec": 0, "max_entry_price": 0.90},
    ("ETH", "15m"): {"entry_window_sec": 45, "threshold": 0.84,
                     "confirm_sec": 10, "max_entry_price": 0.92},
    ("ETH", "5m"): {"entry_window_sec": 15, "threshold": 0.90,
                    "confirm_sec": 0, "max_entry_price": 0.93},
    ("SOL", "15m"): {"entry_window_sec": 45, "threshold": 0.84,
                     "confirm_sec": 0, "max_entry_price": 0.93},
    ("SOL", "5m"): {"entry_window_sec": 20, "threshold": 0.84,
                    "confirm_sec": 0, "max_entry_price": 0.95},
    ("XRP", "15m"): {"entry_window_sec": 60, "threshold": 0.90,
                     "confirm_sec": 10, "max_entry_price": 0.94},
    ("XRP", "5m"): {"entry_window_sec": 20, "threshold": 0.90,
                    "confirm_sec": 0, "max_entry_price": 0.92},
    ("DOGE", "15m"): {"entry_window_sec": 60, "threshold": 0.86,
                      "confirm_sec": 10, "max_entry_price": 0.95},
    ("DOGE", "5m"): {"entry_window_sec": 25, "threshold": 0.84,
                     "confirm_sec": 2, "max_entry_price": 0.98},
    ("BNB", "15m"): {"entry_window_sec": 60, "threshold": 0.84,
                     "confirm_sec": 0, "max_entry_price": 0.96},
    ("BNB", "5m"): {"entry_window_sec": 10, "threshold": 0.84,
                    "confirm_sec": 1, "max_entry_price": 0.95},
    ("HYPE", "15m"): {"entry_window_sec": 105, "threshold": 0.88,
                      "confirm_sec": 10, "max_entry_price": 0.97},
    ("HYPE", "5m"): {"entry_window_sec": 15, "threshold": 0.90,
                     "confirm_sec": 0, "max_entry_price": 0.93},
}

# Assets that have a real backtest report behind their defaults.
RESEARCHED_ASSETS: frozenset[str] = frozenset(
    {"BTC", "ETH", "SOL", "DOGE", "BNB", "HYPE"}
)


def strategy_key(asset: str, period: str) -> str:
    """Stable identifier used by the DB, APIs and dashboard: ``BTC:15m``."""
    return f"{asset.strip().upper()}:{period.strip()}"


def parse_strategy_key(value: str | None) -> tuple[str, str]:
    """Accept ``BTC:15m`` (and legacy ``15m`` / ``5m`` -> BTC)."""
    raw = (value or "").strip()
    if raw in PERIODS:
        return "BTC", raw
    if ":" in raw:
        asset, _, period = raw.partition(":")
        asset = asset.upper()
        if asset in ASSET_ORDER and period in PERIODS:
            return asset, period
    raise ValueError(f"未知策略: {value}")

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
    APP_NAME: str = "PolyTail 多币种"
    ASSET: str = "BTC"
    PERIOD: str = "15m"
    ROUND_PERIOD_S: int = 900

    # paper = simulate fills, record everything, never touch the wallet
    # live  = real orders on Polymarket (GTD maker first, FAK fallback)
    # All seven assets start in paper. The dashboard can switch a single
    # strategy to live at runtime once its credentials and track record are
    # verified.
    TRADING_MODE: str = "paper"

    # ── Strategy (the parameters from docs/optimal_parameters.md) ──────────
    # The generic names configure the **BTC 15m** strategy (the documented,
    # frozen configuration). BTC 5m has its own *_5M overrides; every other
    # asset uses RECOMMENDED_PARAMS via ``for_strategy``.
    ENTRY_WINDOW_SEC: float = 60.0     # only act in the last N seconds
    ENTRY_THRESHOLD: float = 0.94      # first side to reach this triggers
    CONFIRM_SEC: float = 15.0          # wait, then require it is still >= threshold
    MAX_ENTRY_PRICE: float = 0.96      # skip if the confirmed price is >= this
    MIN_ENTRY_PRICE: float = 0.0       # 0 disables
    STAKE_USD: float = 10.0          # backtest notional (10 USDC per trade)
    # Order sizing is by USDC and is configured separately for paper and
    # live execution: a strategy can be tested at 10 U and then promoted to
    # live at a smaller 5 U size without changing its paper record.
    ENTRY_NOTIONAL_USD_PAPER: float = 10.0
    ENTRY_NOTIONAL_USD_LIVE: float = 5.0
    # Legacy/backtest notional. ``for_strategy`` mirrors the paper size here
    # so research and paper execution stay aligned.
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
    # BTC 15m / 5m are the only strategies enabled out of the box. Every
    # other asset is created but idle until enabled from the config page.
    # ENABLED_STRATEGIES can override the default list, e.g.
    # "BTC:15m,BTC:5m,ETH:15m".
    ENABLED_15M: bool = True
    ENABLED_5M: bool = True
    ENABLED_STRATEGIES: str = ""
    # Set per strategy at runtime; not a strategy parameter.
    ENABLED: bool = True
    # Per-strategy mode. Empty = inherit TRADING_MODE. Everything defaults
    # to paper; the operator can promote one strategy at a time.
    TRADING_MODE_5M: str = "paper"

    # BTC 5m defaults are the "稳健版" from docs/optimal_parameters.md:
    # 10s window / 0.88 / no confirmation / 0.90 cap.
    ENTRY_WINDOW_SEC_5M: float = 10.0
    ENTRY_THRESHOLD_5M: float = 0.88
    CONFIRM_SEC_5M: float = 0.0
    MAX_ENTRY_PRICE_5M: float = 0.90
    MIN_ENTRY_PRICE_5M: float = 0.0
    # Backtest stake and live order size share one number so the research
    # results and the deployed strategy cannot drift apart. All strategies
    # start at 10 USDC per trade.
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

    def entry_notional_for(self, mode: str | None = None) -> float:
        """USDC budget for one entry in the given execution mode."""
        resolved = (mode or self.TRADING_MODE or "paper").strip().lower()
        if resolved == "live":
            return float(self.ENTRY_NOTIONAL_USD_LIVE)
        return float(self.ENTRY_NOTIONAL_USD_PAPER)

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

    def _default_enabled(self, asset: str, period: str) -> bool:
        raw = (self.ENABLED_STRATEGIES or "").strip()
        if raw:
            wanted = {part.strip().upper() for part in raw.split(",") if part.strip()}
            key = strategy_key(asset, period)
            return key.upper() in wanted or key in wanted
        if asset == "BTC":
            return bool(self.ENABLED_15M if period == "15m" else self.ENABLED_5M)
        return False

    def for_strategy(self, asset: str, period: str) -> "Settings":
        """Independent settings for one ``ASSET`` x ``period`` strategy.

        Everything (broker mode, stake, strategy params, enabled flag) is per
        strategy, so all seven assets and both periods can share one process
        and one database without touching each other's configuration. Runtime
        overrides are applied to the returned clone by
        ``src/web/runtime_config``.
        """
        asset = (asset or "BTC").strip().upper()
        period = (period or "15m").strip()
        if asset not in ASSET_ORDER:
            raise ValueError(f"不支持的资产: {asset}")
        if period not in PERIODS:
            raise ValueError(f"不支持的周期: {period}")

        clone = self.model_copy(deep=True)
        clone.ASSET = asset
        clone.PERIOD = period

        if asset == "BTC" and period == "15m":
            # Base fields already carry config.toml / .env overrides.
            pass
        elif asset == "BTC" and period == "5m":
            clone.ENTRY_WINDOW_SEC = float(self.ENTRY_WINDOW_SEC_5M)
            clone.ENTRY_THRESHOLD = float(self.ENTRY_THRESHOLD_5M)
            clone.CONFIRM_SEC = float(self.CONFIRM_SEC_5M)
            clone.MAX_ENTRY_PRICE = float(self.MAX_ENTRY_PRICE_5M)
            clone.MIN_ENTRY_PRICE = float(self.MIN_ENTRY_PRICE_5M)
            clone.STAKE_USD = float(self.STAKE_USD_5M)
            clone.ENTRY_NOTIONAL_USD = float(self.ENTRY_NOTIONAL_USD_5M)
            clone.REARM_AFTER_FAILED_CONFIRM = bool(self.REARM_AFTER_FAILED_CONFIRM_5M)
        else:
            defaults = RECOMMENDED_PARAMS[(asset, period)]
            clone.ENTRY_WINDOW_SEC = float(defaults["entry_window_sec"])
            clone.ENTRY_THRESHOLD = float(defaults["threshold"])
            clone.CONFIRM_SEC = float(defaults["confirm_sec"])
            clone.MAX_ENTRY_PRICE = float(defaults["max_entry_price"])
            clone.MIN_ENTRY_PRICE = 0.0
            clone.STAKE_USD = 10.0
            clone.ENTRY_NOTIONAL_USD = 10.0
            clone.REARM_AFTER_FAILED_CONFIRM = False

        if asset == "BTC" and period == "15m":
            mode = self.TRADING_MODE
        elif asset == "BTC" and period == "5m":
            mode = self.TRADING_MODE_5M or self.TRADING_MODE
        else:
            # New assets never go live by accident through an inherited
            # global TRADING_MODE; the operator promotes them explicitly.
            mode = "paper"
        clone.TRADING_MODE = (mode or "paper").strip().lower()
        # Paper and live sizes are separate knobs for every strategy:
        # 10 USDC while simulating, 5 USDC once promoted to real money.
        clone.ENTRY_NOTIONAL_USD_PAPER = float(self.ENTRY_NOTIONAL_USD_PAPER)
        clone.ENTRY_NOTIONAL_USD_LIVE = float(self.ENTRY_NOTIONAL_USD_LIVE)
        # Research/backtest consume the legacy field; keep it aligned with
        # the paper budget (the mode every new strategy starts in).
        clone.ENTRY_NOTIONAL_USD = clone.ENTRY_NOTIONAL_USD_PAPER
        clone.ENABLED = self._default_enabled(asset, period)
        return clone

    def for_period(self, period: str) -> "Settings":
        """Backward-compatible alias for the configured base asset."""
        return self.for_strategy(self.ASSET or "BTC", period)

    def enabled_periods(self) -> list[str]:
        periods = []
        if self.ENABLED_15M:
            periods.append("15m")
        if self.ENABLED_5M:
            periods.append("5m")
        return periods

    def strategy_settings(self) -> dict[str, "Settings"]:
        """{strategy key: Settings} for every supported asset x period."""
        return {
            strategy_key(asset, period): self.for_strategy(asset, period)
            for asset in ASSET_ORDER
            for period in PERIODS
        }

    def enabled_strategy_settings(self) -> dict[str, "Settings"]:
        """Only the strategies enabled by config / env defaults."""
        return {
            key: strategy
            for key, strategy in self.strategy_settings().items()
            if strategy.ENABLED
        }

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        for name in ("ENTRY_WINDOW_SEC", "ENTRY_THRESHOLD", "CONFIRM_SEC",
                     "MAX_ENTRY_PRICE", "MIN_ENTRY_PRICE", "STAKE_USD",
                     "ENTRY_NOTIONAL_USD_PAPER", "ENTRY_NOTIONAL_USD_LIVE",
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
        for name in ("ENTRY_NOTIONAL_USD_PAPER", "ENTRY_NOTIONAL_USD_LIVE",
                     "ENTRY_NOTIONAL_USD"):
            value = float(getattr(self, name))
            if value <= 0:
                errors.append(f"{name} 必须 > 0")
            # A GTD is sized in shares; at the highest allowed price the
            # order must still reach the CLOB's 5-share minimum.
            if (
                self.ENTRY_ORDER_TYPE == "gtd"
                and self.MAX_ENTRY_PRICE > 0
                and value / self.MAX_ENTRY_PRICE < 5
            ):
                errors.append(
                    f"GTD 挂单最少 5 股；请把 {name} 提高到至少 "
                    "5 × 入场价上限，或改用 fak"
                )
        if self.GTD_FILL_TIMEOUT_S <= 0:
            errors.append("GTD_FILL_TIMEOUT_S 必须 > 0")
        if self.MAKER_FEE_RATE < 0 or self.MAKER_FEE_RATE > 0.5:
            errors.append("MAKER_FEE_RATE 必须在 [0, 0.5] 内")
        return errors


# Fields the dashboard / agent may change at runtime, with bounds used by
# both server-side validation and the research optimizer.
TUNABLE_SPECS: dict[str, dict] = {
    "ENABLED": {"group": "strategy", "type": "bool",
                "label": "启用该策略",
                "help": "关闭后不再发现新市场或开仓；已有持仓仍会持有到结算。"},
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
    "ENTRY_NOTIONAL_USD_PAPER": {"group": "sizing", "type": "float",
                                 "min": 1, "max": 100000, "step": 1,
                                 "label": "纸面单笔金额(USDC)",
                                 "help": "纸面模式每笔投入；14 个策略共用；默认 10U。"},
    "ENTRY_NOTIONAL_USD_LIVE": {"group": "sizing", "type": "float",
                                "min": 1, "max": 100000, "step": 1,
                                "label": "实盘单笔金额(USDC)",
                                "help": "实盘模式每笔投入；14 个策略共用；默认 5U。"},
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

