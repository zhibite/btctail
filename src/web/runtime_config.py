"""Runtime-tunable strategy parameters, scoped per strategy (15m / 5m).

Only the keys in ``config.TUNABLE_SPECS`` can be changed from the dashboard.
Each strategy has its own ``Settings`` clone and its own row namespace in the
``settings`` table, so tuning the 5m strategy never touches the frozen 15m
configuration. Changes are validated against both the per-field bounds and
the whole strategy's consistency rules, persisted, and applied to the live
clone in place -- the next window picks them up without a restart.
"""
from __future__ import annotations

import math
from typing import Any

from src.config import TUNABLE_SPECS, Settings

_BOOL_TRUE = {"1", "true", "yes", "on"}


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
        value = str(raw)
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
    return settings


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
    errors = candidate.validation_errors()
    if errors:
        return False, errors, {}

    for key, value in coerced.items():
        setattr(settings, key, value)
    db.settings_set_many({k: str(v) for k, v in coerced.items()}, scope=scope)
    return True, [], coerced


def reset(settings: Settings, db, scope: str) -> Settings:
    """Drop this strategy's overrides and rebuild it from config.toml + env."""
    db.settings_delete(list(TUNABLE_SPECS.keys()), scope=scope)
    from src.config import build

    fresh = build().for_period(scope)
    for name in Settings.model_fields:
        try:
            setattr(settings, name, getattr(fresh, name))
        except Exception:  # noqa: BLE001
            continue
    return settings


def payload(settings: Settings, db, scope: str) -> dict:
    overrides = db.settings_all(scope)
    return {
        "scope": scope,
        "values": {key: getattr(settings, key) for key in TUNABLE_SPECS},
        "specs": TUNABLE_SPECS,
        "overrides": {k: v for k, v in overrides.items() if k in TUNABLE_SPECS},
        "errors": settings.validation_errors(),
        "mode": settings.TRADING_MODE,
        "paper": settings.is_paper,
        "asset": settings.ASSET,
        "period": settings.PERIOD,
        "stake_usd": settings.STAKE_USD,
    }
