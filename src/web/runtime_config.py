"""Runtime-tunable strategy parameters, scoped per ``ASSET:period``.

Only the keys in ``config.TUNABLE_SPECS`` can be changed from the dashboard.
Each strategy has its own ``Settings`` clone and its own row namespace in the
``settings`` table, so tuning BTC 5m never touches ETH 5m or the frozen BTC
15m configuration. Changes are validated against both the per-field bounds
and the whole strategy's consistency rules, persisted, and applied to the
live clone in place -- the next window picks them up without a restart.
"""
from __future__ import annotations

import math
from typing import Any

from src.config import (
    PERIODS,
    RECOMMENDED_PARAMS,
    RESEARCHED_ASSETS,
    TUNABLE_SPECS,
    Settings,
    parse_strategy_key,
    strategy_key,
)

_BOOL_TRUE = {"1", "true", "yes", "on"}
MODE_KEY = "TRADING_MODE"   # persisted per scope; not agent-tunable
SHARED_SCOPE = "shared"     # execution / risk parameters common to all strategies
SHARED_GROUPS = ("execution", "risk")
SHARED_KEYS = tuple(
    key for key, spec in TUNABLE_SPECS.items()
    if spec.get("group") in SHARED_GROUPS
)


def _scope_of(runtime: Any) -> str:
    return getattr(
        runtime, "key",
        strategy_key(runtime.settings.ASSET, runtime.settings.PERIOD),
    )


def coerce(key: str, raw: Any) -> Any:
    spec = TUNABLE_SPECS[key]
    kind = spec["type"]
    if kind in ("int", "float"):
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是数字") from None
        # NaN compares False against both bounds, so it would slip through a
        # plain min/max check and then poison every downstream comparison.
        if not math.isfinite(number):
            raise ValueError(f"{key} 必须是有限数字")
        value = int(number) if kind == "int" else number
    elif kind == "bool":
        value = raw if isinstance(raw, bool) else str(raw).strip().lower() in _BOOL_TRUE
    else:
        value = str(raw).strip().lower()
        options = spec.get("options")
        if options and value not in options:
            raise ValueError(f"{key} 必须是 {'/'.join(options)} 之一")
    low, high = spec.get("min"), spec.get("max")
    if low is not None and value < low:
        raise ValueError(f"{key}={value} 低于下限 {low}")
    if high is not None and value > high:
        raise ValueError(f"{key}={value} 高于上限 {high}")
    return value


def apply_overrides(settings: Settings, db, scope: str = "15m") -> Settings:
    """Mutate a strategy's Settings clone from the DB overrides."""
    for key, raw in db.settings_all(scope).items():
        if key not in TUNABLE_SPECS:
            continue
        try:
            setattr(settings, key, coerce(key, raw))
        except (TypeError, ValueError):
            continue
    # Research/backtest sizing follows the paper budget.
    settings.ENTRY_NOTIONAL_USD = float(settings.ENTRY_NOTIONAL_USD_PAPER)
    return settings


def apply_shared_overrides(settings: Settings, db) -> Settings:
    """Apply execution/risk overrides shared by every strategy."""
    for key, raw in db.settings_all(SHARED_SCOPE).items():
        if key not in SHARED_KEYS:
            continue
        try:
            setattr(settings, key, coerce(key, raw))
        except (TypeError, ValueError):
            continue
    return settings


def shared_payload(settings: Settings, db) -> dict:
    overrides = db.settings_all(SHARED_SCOPE)
    return {
        "scope": SHARED_SCOPE,
        "values": {key: getattr(settings, key) for key in SHARED_KEYS},
        "specs": {key: TUNABLE_SPECS[key] for key in SHARED_KEYS},
        "overrides": {k: v for k, v in overrides.items() if k in SHARED_KEYS},
        "errors": settings.validation_errors(),
    }


def update_shared(runtimes: dict[str, Any], db, values: dict[str, Any]
                  ) -> tuple[bool, list[str], dict]:
    """Validate + persist execution/risk parameters for every strategy."""
    unknown = [key for key in values if key not in SHARED_KEYS]
    if unknown:
        return False, [f"不是共有参数: {', '.join(unknown)}"], {}

    coerced: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in values.items():
        try:
            coerced[key] = coerce(key, raw)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
    if errors:
        return False, errors, {}
    if not coerced:
        return True, [], {}

    # Validate against every strategy: the shared order type interacts with
    # each strategy's own entry price cap and live notional (5-share GTD
    # minimum), so one invalid strategy must reject the whole change.
    for key, runtime in runtimes.items():
        if not bool(getattr(runtime.settings, "ENABLED", True)):
            continue
        candidate = runtime.settings.model_copy(deep=True)
        for name, value in coerced.items():
            setattr(candidate, name, value)
        candidate.ENTRY_NOTIONAL_USD = float(candidate.ENTRY_NOTIONAL_USD_PAPER)
        problems = candidate.validation_errors()
        if problems:
            return False, [f"{key}: {message}" for message in problems], {}

    for runtime in runtimes.values():
        for name, value in coerced.items():
            setattr(runtime.settings, name, value)
    db.settings_set_many({k: str(v) for k, v in coerced.items()},
                         scope=SHARED_SCOPE)
    # Remove stale per-strategy copies so the shared value is the only
    # source of truth after a restart.
    for runtime in runtimes.values():
        db.settings_delete(list(coerced.keys()), scope=_scope_of(runtime))
    return True, [], coerced


def update(settings: Settings, db, scope: str, values: dict[str, Any]
           ) -> tuple[bool, list[str], dict]:
    """Validate + persist a batch of overrides for one strategy."""
    unknown = [k for k in values if k not in TUNABLE_SPECS]
    if unknown:
        return False, [f"不可修改的参数: {', '.join(unknown)}"], {}

    coerced: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in values.items():
        try:
            coerced[key] = coerce(key, raw)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
    if errors:
        return False, errors, {}

    candidate = settings.model_copy(deep=True)
    for key, value in coerced.items():
        setattr(candidate, key, value)
    candidate.ENTRY_NOTIONAL_USD = float(candidate.ENTRY_NOTIONAL_USD_PAPER)
    # Disabling must always be possible, even if the strategy's saved
    # parameters are currently invalid (that is exactly when a kill switch
    # matters). Any other change is validated in full.
    disabling_only = set(coerced) == {"ENABLED"} and coerced["ENABLED"] is False
    errors = [] if disabling_only else candidate.validation_errors()
    if errors:
        return False, errors, {}

    for key, value in coerced.items():
        setattr(settings, key, value)
    settings.ENTRY_NOTIONAL_USD = float(settings.ENTRY_NOTIONAL_USD_PAPER)
    db.settings_set_many({k: str(v) for k, v in coerced.items()}, scope=scope)
    return True, [], coerced


def reset(settings: Settings, db, scope: str) -> Settings:
    """Drop this strategy's overrides and rebuild it from config.toml + env.

    The paper/live mode and enabled flag are preserved: both have side
    effects (broker rebuild, window lifecycle) and stay explicit actions.
    """
    reset_keys = [key for key in TUNABLE_SPECS if key != "ENABLED"]
    db.settings_delete(reset_keys, scope=scope)
    from src.config import build

    asset, period = parse_strategy_key(scope)
    fresh = build().for_strategy(asset, period)
    mode = settings.TRADING_MODE
    enabled = bool(getattr(settings, "ENABLED", True))
    for name in Settings.model_fields:
        try:
            setattr(settings, name, getattr(fresh, name))
        except Exception:  # noqa: BLE001
            continue
    settings.TRADING_MODE = mode
    settings.ENABLED = enabled
    # Common execution/risk values are not per-strategy overrides.
    apply_shared_overrides(settings, db)
    return settings


def payload(settings: Settings, db, scope: str) -> dict:
    overrides = db.settings_all(scope)
    asset, period = parse_strategy_key(scope)
    recommended = RECOMMENDED_PARAMS.get((asset, period), {})
    return {
        "scope": scope,
        "key": strategy_key(asset, period),
        "values": {key: getattr(settings, key) for key in TUNABLE_SPECS},
        "specs": TUNABLE_SPECS,
        "overrides": {k: v for k, v in overrides.items() if k in TUNABLE_SPECS},
        "errors": settings.validation_errors(),
        "mode": settings.TRADING_MODE,
        "paper": settings.is_paper,
        "asset": asset,
        "period": period,
        "enabled": bool(getattr(settings, "ENABLED", True)),
        "researched": asset in RESEARCHED_ASSETS,
        "stake_usd": settings.STAKE_USD,
        "recommended": {
            "ENTRY_WINDOW_SEC": recommended.get("entry_window_sec"),
            "ENTRY_THRESHOLD": recommended.get("threshold"),
            "CONFIRM_SEC": recommended.get("confirm_sec"),
            "MAX_ENTRY_PRICE": recommended.get("max_entry_price"),
            "ENTRY_NOTIONAL_USD_PAPER": 10.0,
            "ENTRY_NOTIONAL_USD_LIVE": 5.0,
        },
        "periods": list(PERIODS),
    }


def apply_mode_override(settings: Settings, db, scope: str) -> Settings:
    """Load the operator's paper/live choice saved from the dashboard."""
    value = db.settings_all(scope).get(MODE_KEY)
    if value in ("paper", "live"):
        settings.TRADING_MODE = value
    return settings


def save_mode(db, scope: str, mode: str) -> None:
    db.settings_set_many({MODE_KEY: mode}, scope=scope)
