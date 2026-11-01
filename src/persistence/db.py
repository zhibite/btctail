"""SQLite store for windows, signals, trades, settings and the event journal.

Every market row is scoped by ``period`` ("15m" / "5m") because both
strategies run in one process and their 15m and 5m windows can share the same
slot number. Settings are scoped the same way, so each strategy carries its
own runtime overrides.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from src.infra.logging import get_logger
from src.market.models import MarketInfo, Side

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    slot          INTEGER NOT NULL,
    period        TEXT NOT NULL,
    asset         TEXT NOT NULL,
    slug          TEXT DEFAULT '',
    condition_id  TEXT DEFAULT '',
    up_token      TEXT DEFAULT '',
    down_token    TEXT DEFAULT '',
    end_ts        INTEGER DEFAULT 0,
    tick_size     REAL DEFAULT 0.01,
    resolved      TEXT DEFAULT '',
    resolved_at   REAL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (slot, period)
);

CREATE TABLE IF NOT EXISTS signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    slot              INTEGER NOT NULL,
    period            TEXT NOT NULL,
    mode              TEXT NOT NULL,
    side              TEXT DEFAULT '',
    trigger_ts        REAL,
    trigger_price     REAL,
    trigger_remaining REAL,
    confirm_due_ts    REAL,
    confirm_ts        REAL,
    confirm_price     REAL,
    decision          TEXT NOT NULL,
    skip_reason       TEXT DEFAULT '',
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_slot ON signals(slot, period);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slot          INTEGER NOT NULL,
    period        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    side          TEXT NOT NULL,
    token_id      TEXT DEFAULT '',
    condition_id  TEXT DEFAULT '',
    signal_price  REAL DEFAULT 0,
    entry_price   REAL DEFAULT 0,
    shares        REAL DEFAULT 0,
    cost          REAL DEFAULT 0,
    fee           REAL DEFAULT 0,
    fee_rate      REAL DEFAULT 0,
    stake         REAL DEFAULT 0,
    order_id      TEXT DEFAULT '',
    status        TEXT NOT NULL,
    payout        REAL DEFAULT 0,
    pnl           REAL DEFAULT 0,
    entry_ts      REAL NOT NULL,
    settled_at    REAL,
    resolved_side TEXT DEFAULT '',
    redeem_status TEXT DEFAULT '',
    error         TEXT DEFAULT '',
    extra         TEXT DEFAULT '',
    created_at    REAL NOT NULL,
    UNIQUE(slot, period, mode)
);
CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_ts);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_period ON trades(period);

CREATE TABLE IF NOT EXISTS settings (
    scope      TEXT NOT NULL DEFAULT '15m',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    level      TEXT NOT NULL,
    event      TEXT NOT NULL,
    data       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS research_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    period      TEXT DEFAULT '15m',
    status      TEXT NOT NULL,
    params      TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    report      TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    started_at  REAL NOT NULL,
    finished_at REAL
);
"""

# Tables that need the period/scope columns when migrating from the
# single-strategy schema.
_PERIOD_TABLES = ("windows", "signals", "trades", "settings", "research_runs")


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


class DB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate_single_strategy_schema()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── migration ─────────────────────────────────────────────────────────

    def _migrate_single_strategy_schema(self) -> None:
        """Rename the pre-multi-strategy tables instead of silently failing.

        The old schema keyed trades on (slot, mode); with 5m and 15m running
        together a slot can belong to both. Renaming keeps the old rows
        inspectable while the new schema is created.
        """
        try:
            tables = {
                row["name"] for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "trades" not in tables:
                return
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(trades)")
            }
            if "period" in columns:
                return
            suffix = f"_legacy_{int(time.time())}"
            for name in _PERIOD_TABLES:
                if name in tables:
                    self._conn.execute(f"ALTER TABLE {name} RENAME TO {name}{suffix}")
            self._conn.commit()
            log.warning(
                "db.schema_migrated",
                renamed_suffix=suffix,
                note="旧版单策略表已重命名保留；新表按 period 分区",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("db.migration_failed", error=str(exc))

    # ── windows ───────────────────────────────────────────────────────────

    def upsert_window(self, market: MarketInfo) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO windows (slot, period, asset, slug, condition_id,
                       up_token, down_token, end_ts, tick_size, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slot, period) DO UPDATE SET
                       slug=excluded.slug,
                       condition_id=excluded.condition_id,
                       up_token=excluded.up_token,
                       down_token=excluded.down_token,
                       end_ts=excluded.end_ts,
                       tick_size=excluded.tick_size""",
                (market.slot, market.period, market.asset, market.slug,
                 market.condition_id, market.token_up, market.token_down,
                 market.end_ts, market.tick_size, time.time()),
            )
            self._conn.commit()

    def set_window_resolution(self, slot: int, period: str, side: Side,
                              resolved_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE windows SET resolved=?, resolved_at=? WHERE slot=? AND period=?",
                (side.value, resolved_at, slot, period),
            )
            self._conn.commit()

    def get_window(self, slot: int, period: str = "15m") -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM windows WHERE slot=? AND period=?", (slot, period)
            ).fetchone()
        return _row_to_dict(row)

    def recent_windows(self, limit: int = 50, period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM windows"
        args: list[Any] = []
        if period:
            sql += " WHERE period=?"
            args.append(period)
        sql += " ORDER BY slot DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return _rows_to_dicts(rows)

    # ── signals ───────────────────────────────────────────────────────────

    def add_signal(
        self,
        *,
        slot: int,
        period: str,
        mode: str,
        decision: str,
        side: str = "",
        trigger_ts: float | None = None,
        trigger_price: float | None = None,
        trigger_remaining: float | None = None,
        confirm_due_ts: float | None = None,
        confirm_ts: float | None = None,
        confirm_price: float | None = None,
        skip_reason: str = "",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO signals (slot, period, mode, side, trigger_ts,
                       trigger_price, trigger_remaining, confirm_due_ts,
                       confirm_ts, confirm_price, decision, skip_reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (slot, period, mode, side, trigger_ts, trigger_price,
                 trigger_remaining, confirm_due_ts, confirm_ts, confirm_price,
                 decision, skip_reason, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent_signals(self, limit: int = 100, mode: str | None = None,
                       period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM signals WHERE 1=1"
        args: list[Any] = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if period:
            sql += " AND period=?"
            args.append(period)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return _rows_to_dicts(rows)

    # ── trades ────────────────────────────────────────────────────────────

    def trade_exists(self, slot: int, mode: str, period: str = "15m") -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM trades WHERE slot=? AND mode=? AND period=? LIMIT 1",
                (slot, mode, period),
            ).fetchone()
        return row is not None

    def create_trade(
        self,
        *,
        slot: int,
        period: str,
        mode: str,
        side: str,
        token_id: str,
        condition_id: str,
        signal_price: float,
        entry_price: float,
        shares: float,
        cost: float,
        fee: float,
        fee_rate: float,
        stake: float,
        order_id: str,
        status: str = "open",
        entry_ts: float | None = None,
        extra: dict | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO trades (slot, period, mode, side, token_id,
                       condition_id, signal_price, entry_price, shares, cost,
                       fee, fee_rate, stake, order_id, status, entry_ts, extra,
                       created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slot, period, mode) DO NOTHING""",
                (slot, period, mode, side, token_id, condition_id, signal_price,
                 entry_price, shares, cost, fee, fee_rate, stake, order_id,
                 status, entry_ts or time.time(),
                 json.dumps(extra or {}, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def settle_trade(
        self, trade_id: int, *, resolved_side: Side | str, pnl: float, payout: float,
        status: str | None = None,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT side FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
            if row is None:
                return
            resolved_value = (
                resolved_side.value if isinstance(resolved_side, Side)
                else str(resolved_side).upper()
            )
            final = status or ("won" if row["side"] == resolved_value else "lost")
            self._conn.execute(
                """UPDATE trades SET status=?, payout=?, pnl=?, settled_at=?,
                       resolved_side=? WHERE id=?""",
                (final, payout, pnl, time.time(), resolved_value, trade_id),
            )
            self._conn.commit()

    def set_redeem_status(self, trade_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET redeem_status=? WHERE id=?", (status, trade_id)
            )
            self._conn.commit()

    def unredeemed_trades(self, period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE status='won' AND redeem_status='needed'"
        args: list[Any] = []
        if period:
            sql += " AND period=?"
            args.append(period)
        sql += " ORDER BY entry_ts"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return _rows_to_dicts(rows)

    def mark_trade_error(self, trade_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET status='error', error=? WHERE id=?", (error, trade_id)
            )
            self._conn.commit()

    def get_trade(self, trade_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
        return _row_to_dict(row)

    def open_trades(self, mode: str | None = None,
                    period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE status='open'"
        args: list[Any] = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if period:
            sql += " AND period=?"
            args.append(period)
        sql += " ORDER BY slot"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return _rows_to_dicts(rows)

    def recent_trades(self, limit: int = 100, mode: str | None = None,
                      period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE 1=1"
        args: list[Any] = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if period:
            sql += " AND period=?"
            args.append(period)
        sql += " ORDER BY entry_ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return _rows_to_dicts(rows)

    def trades_between(self, start_ts: float, end_ts: float, mode: str | None = None,
                       period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE entry_ts >= ? AND entry_ts < ?"
        args: list[Any] = [start_ts, end_ts]
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if period:
            sql += " AND period=?"
            args.append(period)
        sql += " ORDER BY entry_ts"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return _rows_to_dicts(rows)

    def all_settled_trades(self, mode: str | None = None,
                           period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE status IN ('won','lost','void')"
        args: list[Any] = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if period:
            sql += " AND period=?"
            args.append(period)
        sql += " ORDER BY entry_ts"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return _rows_to_dicts(rows)

    def delete_trade(self, trade_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
            self._conn.commit()

    # ── settings (runtime overrides, scoped by strategy) ─────────────────

    def settings_all(self, scope: str = "15m") -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM settings WHERE scope=?", (scope,)
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def settings_set_many(self, values: dict[str, str], scope: str = "15m") -> None:
        now = time.time()
        with self._lock:
            self._conn.executemany(
                """INSERT INTO settings (scope, key, value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,
                       updated_at=excluded.updated_at""",
                [(scope, k, str(v), now) for k, v in values.items()],
            )
            self._conn.commit()

    def settings_delete(self, keys: Iterable[str], scope: str = "15m") -> None:
        keys = list(keys)
        if not keys:
            return
        with self._lock:
            self._conn.executemany(
                "DELETE FROM settings WHERE scope=? AND key=?",
                [(scope, k) for k in keys],
            )
            self._conn.commit()

    # ── events ────────────────────────────────────────────────────────────

    def add_event(self, level: str, event: str, data: dict | None = None) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events (ts, level, event, data) VALUES (?,?,?,?)",
                    (time.time(), level, event,
                     json.dumps(data or {}, ensure_ascii=False, default=str)),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def recent_events(self, limit: int = 200, min_level: str = "INFO") -> list[dict]:
        order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
        floor = order.get(min_level.upper(), 1)
        levels = [name for name, rank in order.items() if rank >= floor]
        placeholders = ",".join("?" for _ in levels)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE level IN ({placeholders}) "
                "ORDER BY id DESC LIMIT ?",
                (*levels, limit),
            ).fetchall()
        out = []
        for row in _rows_to_dicts(rows):
            try:
                row["data"] = json.loads(row["data"]) if row["data"] else {}
            except Exception:  # noqa: BLE001
                row["data"] = {}
            out.append(row)
        return out

    def prune_events(self, keep_days: int = 14) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ── runtime state (killswitch, pause, misc) ───────────────────────────

    def state_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:  # noqa: BLE001
            return default

    def state_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO state (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                       updated_at=excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False, default=str), time.time()),
            )
            self._conn.commit()

    def state_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM state WHERE key=?", (key,))
            self._conn.commit()

    # ── research runs ─────────────────────────────────────────────────────

    def create_research_run(self, kind: str, params: dict, period: str = "15m") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO research_runs (kind, period, status, params, started_at) "
                "VALUES (?,?,?,?,?)",
                (kind, period, "running", json.dumps(params, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def finish_research_run(self, run_id: int, *, status: str, summary: dict | None = None,
                            report: str = "", error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE research_runs SET status=?, summary=?, report=?, error=?, "
                "finished_at=? WHERE id=?",
                (status, json.dumps(summary or {}, ensure_ascii=False, default=str),
                 report, error, time.time(), run_id),
            )
            self._conn.commit()

    def fail_stale_research_runs(self, note: str = "interrupted by restart") -> int:
        """Mark runs left 'running' by a killed process as failed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE research_runs SET status='error', error=?, finished_at=? "
                "WHERE status='running'",
                (note, time.time()),
            )
            self._conn.commit()
            return cur.rowcount

    def get_research_run(self, run_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM research_runs WHERE id=?", (run_id,)
            ).fetchone()
        item = _row_to_dict(row)
        if item:
            for key in ("params", "summary"):
                try:
                    item[key] = json.loads(item[key]) if item[key] else {}
                except Exception:  # noqa: BLE001
                    item[key] = {}
        return item

    def recent_research_runs(self, limit: int = 20, period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM research_runs"
        args: list[Any] = []
        if period:
            sql += " WHERE period=?"
            args.append(period)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for item in _rows_to_dicts(rows):
            for key in ("params", "summary"):
                try:
                    item[key] = json.loads(item[key]) if item[key] else {}
                except Exception:  # noqa: BLE001
                    item[key] = {}
            out.append(item)
        return out


# ---------------------------------------------------------------------------
# Aggregations over trade rows
# ---------------------------------------------------------------------------


def compute_stats(trades: list[dict]) -> dict:
    """Headline metrics over settled trades (open rows are ignored)."""
    settled = [t for t in trades if t.get("status") in ("won", "lost", "void")]
    if not settled:
        return {
            "trades": 0, "wins": 0, "losses": 0, "open": 0, "stake": 0.0,
            "cost": 0.0, "fees": 0.0, "pnl": 0.0, "roi": 0.0, "win_rate": 0.0,
            "avg_entry_price": 0.0, "price_edge": 0.0, "avg_pnl": 0.0,
            "max_drawdown": 0.0, "t_stat": None, "breakeven_win_rate": None,
            "profit_factor": None,
        }
    settled.sort(key=lambda t: t.get("entry_ts") or 0)
    # ROI denominator is the capital actually deployed (cost), matching the
    # research backtest; fees are already inside net PnL.
    cost = sum(float(t.get("cost") or 0) for t in settled)
    fees = sum(float(t.get("fee") or 0) for t in settled)
    stake = cost
    pnls = [float(t.get("pnl") or 0) for t in settled]
    pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls)
    avg_price = sum(float(t.get("entry_price") or 0) for t in settled) / len(settled)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_loss = abs(sum(losses))
    n = len(pnls)
    mean = pnl / n
    if n > 1:
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        std = math.sqrt(var)
        t_stat = mean / (std / math.sqrt(n)) if std > 0 else None
    else:
        t_stat = None
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "open": sum(1 for t in trades if t.get("status") == "open"),
        "stake": stake,
        "cost": cost,
        "fees": fees,
        "pnl": pnl,
        "roi": pnl / stake if stake else 0.0,
        "win_rate": win_rate,
        "avg_entry_price": avg_price,
        "price_edge": win_rate - avg_price,
        "avg_pnl": mean,
        "max_drawdown": max_dd,
        "t_stat": t_stat,
        "breakeven_win_rate": (
            abs(avg_loss) / (avg_win + abs(avg_loss)) if (avg_win + abs(avg_loss)) > 0 else None
        ),
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
    }


def equity_curve(trades: list[dict]) -> list[dict]:
    settled = [t for t in trades if t.get("status") in ("won", "lost", "void")]
    settled.sort(key=lambda t: t.get("entry_ts") or 0)
    out = []
    cumulative = 0.0
    peak = 0.0
    for trade in settled:
        pnl = float(trade.get("pnl") or 0)
        cumulative += pnl
        peak = max(peak, cumulative)
        out.append({
            "ts": trade.get("entry_ts"),
            "slot": trade.get("slot"),
            "pnl": pnl,
            "cumulative": cumulative,
            "drawdown": cumulative - peak,
        })
    return out
