"""Runtime config validation and dashboard API (multi-strategy)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.persistence.db import DB
from src.strategy.risk import RiskManager
from src.web import runtime_config
from src.web.app import app, set_state


def test_validation_rejects_inconsistent_cap():
    settings = Settings(ENTRY_THRESHOLD=0.95, MAX_ENTRY_PRICE=0.94)
    errors = settings.validation_errors()
    assert any("MAX_ENTRY_PRICE" in message for message in errors)


def test_validation_rejects_bad_mid_cross_band():
    settings = Settings(MID_CROSS_BAND=0.5)
    errors = settings.validation_errors()
    assert any("MID_CROSS_BAND" in message for message in errors)


def test_mode_defaults():
    settings = Settings()
    # Everything, BTC included, starts in paper until promoted explicitly.
    assert settings.TRADING_MODE == "paper"
    assert settings.TRADING_MODE_5M == "paper"
    assert settings.for_period("15m").TRADING_MODE == "paper"
    assert settings.for_period("5m").TRADING_MODE == "paper"
    # 5m sizing is 10 USDC for both the backtest and the live order.
    assert settings.STAKE_USD_5M == 10.0
    assert settings.ENTRY_NOTIONAL_USD_5M == 10.0
    five = settings.for_period("5m")
    assert five.STAKE_USD == 10.0
    assert five.ENTRY_NOTIONAL_USD == 10.0


def test_documented_defaults_and_enabled_set():
    settings = Settings()
    btc15 = settings.for_strategy("BTC", "15m")
    assert (btc15.ENTRY_WINDOW_SEC, btc15.ENTRY_THRESHOLD,
            btc15.CONFIRM_SEC, btc15.MAX_ENTRY_PRICE) == (60, 0.94, 15, 0.96)
    eth15 = settings.for_strategy("ETH", "15m")
    assert (eth15.ENTRY_WINDOW_SEC, eth15.ENTRY_THRESHOLD,
            eth15.CONFIRM_SEC, eth15.MAX_ENTRY_PRICE) == (45, 0.84, 10, 0.92)
    doge5 = settings.for_strategy("DOGE", "5m")
    assert (doge5.ENTRY_WINDOW_SEC, doge5.ENTRY_THRESHOLD,
            doge5.CONFIRM_SEC, doge5.MAX_ENTRY_PRICE) == (25, 0.84, 2, 0.98)
    # 1s sampling + 0.02 deadband, skip after >5 effective 0.5 flips.
    assert (doge5.MAX_MID_CROSSINGS, doge5.MID_CROSS_BAND,
            doge5.MID_CROSS_SAMPLE_SEC) == (5, 0.02, 1)
    assert btc15.MAX_MID_CROSSINGS == 5
    # Every 15m strategy samples the 900s window every 5s (180 points).
    assert btc15.MID_CROSS_SAMPLE_SEC == 5
    assert eth15.MID_CROSS_SAMPLE_SEC == 5
    sol15 = settings.for_strategy("SOL", "15m")
    assert (sol15.MAX_MID_CROSSINGS, sol15.MID_CROSS_BAND,
            sol15.MID_CROSS_SAMPLE_SEC) == (5, 0.02, 5)
    sol5 = settings.for_strategy("SOL", "5m")
    assert (sol5.MAX_MID_CROSSINGS, sol5.MID_CROSS_BAND,
            sol5.MID_CROSS_SAMPLE_SEC) == (5, 0.02, 1)
    all_settings = settings.strategy_settings()
    assert len(all_settings) == 14
    enabled = settings.enabled_strategy_settings()
    assert set(enabled) == {"BTC:15m", "BTC:5m"}
    for strategy in all_settings.values():
        assert strategy.TRADING_MODE == "paper"
        assert strategy.ENTRY_NOTIONAL_USD_PAPER == 10.0
        assert strategy.ENTRY_NOTIONAL_USD_LIVE == 5.0
        assert strategy.entry_notional_for("paper") == 10.0
        assert strategy.entry_notional_for("live") == 5.0
        # The crossing filter is on for every strategy by default; 15m
        # samples 180 points (5s), 5m samples 1s.
        assert (strategy.MAX_MID_CROSSINGS, strategy.MID_CROSS_BAND) == (5, 0.02)
        assert strategy.MID_CROSS_SAMPLE_SEC == (
            5 if strategy.PERIOD == "15m" else 1
        )


def test_mode_is_scoped_per_asset_and_period(tmp_path):
    db = DB(tmp_path / "mode.db")
    base = Settings()
    strategies = base.strategy_settings()
    eth5 = strategies["ETH:5m"]
    runtime_config.save_mode(db, "ETH:5m", "live")
    runtime_config.apply_mode_override(eth5, db, "ETH:5m")
    assert eth5.TRADING_MODE == "live"
    # BTC and ETH 15m remain untouched.
    assert strategies["BTC:5m"].TRADING_MODE == "paper"
    assert strategies["ETH:15m"].TRADING_MODE == "paper"
    db.close()


def test_enabled_strategies_env_override(monkeypatch):
    monkeypatch.setenv("ENABLED_STRATEGIES", "BTC:15m,ETH:5m")
    settings = Settings()
    enabled = settings.enabled_strategy_settings()
    assert set(enabled) == {"BTC:15m", "ETH:5m"}
    assert enabled["ETH:5m"].ASSET == "ETH"


def test_gtd_needs_five_shares():
    # At the 0.94 cap, 1 USDC converts to ~1.06 shares: below the CLOB's
    # 5-share minimum for a resting order.
    settings = Settings(ENTRY_ORDER_TYPE="gtd", ENTRY_NOTIONAL_USD=1,
                        ENTRY_WINDOW_SEC=60, CONFIRM_SEC=10)
    assert any("5 股" in message for message in settings.validation_errors())
    settings = Settings(ENTRY_ORDER_TYPE="gtd", ENTRY_NOTIONAL_USD=10,
                        ENTRY_WINDOW_SEC=60, CONFIRM_SEC=10)
    assert settings.validation_errors() == []


def test_coerce_bounds():
    with pytest.raises(ValueError):
        runtime_config.coerce("ENTRY_THRESHOLD", 0.5)
    with pytest.raises(ValueError):
        runtime_config.coerce("ENTRY_THRESHOLD", 1.5)
    assert runtime_config.coerce("ENTRY_WINDOW_SEC", "75") == 75
    assert runtime_config.coerce("ENTRY_THRESHOLD", "0.92") == 0.92


def test_non_finite_values_are_rejected():
    # NaN passes both "< min" and "> max"; it must be rejected explicitly.
    for value in ("NaN", "nan", "inf", "-Infinity"):
        with pytest.raises(ValueError):
            runtime_config.coerce("CONFIRM_SEC", value)
    settings = Settings(CONFIRM_SEC=float("nan"))
    assert any("有限" in message for message in settings.validation_errors())


def test_update_and_reset(tmp_path):
    db = DB(tmp_path / "c.db")
    settings = Settings()
    ok, errors, applied = runtime_config.update(
        settings, db, "15m", {"ENTRY_THRESHOLD": 0.92, "ENTRY_WINDOW_SEC": 75}
    )
    assert ok, errors
    assert settings.ENTRY_THRESHOLD == 0.92
    assert db.settings_all("15m")["ENTRY_THRESHOLD"] == "0.92"
    # 5m overrides live in their own scope.
    assert "ENTRY_THRESHOLD" not in db.settings_all("5m")

    ok, errors, _ = runtime_config.update(settings, db, "15m", {"MAX_ENTRY_PRICE": 0.91})
    assert not ok and errors  # below the threshold

    runtime_config.reset(settings, db, "15m")
    assert settings.ENTRY_THRESHOLD != 0.92
    assert "ENTRY_THRESHOLD" not in db.settings_all("15m")
    db.close()


def test_for_period_defaults():
    base = Settings()
    five = base.for_period("5m")
    assert five.PERIOD == "5m"
    assert five.window_seconds == 300
    assert five.ENTRY_WINDOW_SEC == base.ENTRY_WINDOW_SEC_5M
    assert five.MAX_ENTRY_PRICE == base.MAX_ENTRY_PRICE_5M
    assert five.STAKE_USD == base.STAKE_USD_5M
    assert base.for_period("15m").ENTRY_WINDOW_SEC == base.ENTRY_WINDOW_SEC


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


class StubBroker:
    def __init__(self, mode: str = "paper"):
        self.mode = mode

    async def balance(self):
        return 1000.0


class StubRuntime:
    def __init__(self, settings: Settings, db: DB):
        self.settings = settings
        self.key = f"{settings.ASSET}:{settings.PERIOD}"
        self.asset = settings.ASSET
        self.period = settings.PERIOD
        self.risk = RiskManager(db, settings, period=settings.PERIOD,
                                mode="paper", asset=settings.ASSET, scope=self.key)
        self.broker = StubBroker()
        self._started_at = time.time()
        self._last_balance_ts = time.time()

    def set_mode(self, mode):
        self.broker.mode = mode
        self.settings.TRADING_MODE = mode
        return True, ""

    async def set_enabled(self, enabled):
        self.settings.ENABLED = bool(enabled)

    def status(self, **kwargs):
        return {
            "app": {"mode": self.broker.mode, "paper": self.broker.mode == "paper",
                    "uptime_s": 1.0, "mode_fallback": "", "key": self.key,
                    "enabled": self.settings.ENABLED,
                    "period": self.settings.PERIOD, "asset": self.settings.ASSET},
            "params": {
                "exec_slippage": self.settings.EXEC_SLIPPAGE,
                "entry_notional_paper": self.settings.ENTRY_NOTIONAL_USD_PAPER,
                "entry_notional_live": self.settings.ENTRY_NOTIONAL_USD_LIVE,
            },
            "window": None, "strategy": None, "quote": None,
            "risk": self.risk.snapshot(), "balance": 1000.0,
            "stats": {"trades": 0}, "equity": [], "recent_trades": [],
            "recent_signals": [], "unredeemed": [],
        }


class StubResearch:
    def status(self, run_id=None):
        return {"busy": False, "job": None}


class StubState:
    def __init__(self, tmp_path):
        self.settings = Settings(WEB_PASSWORD="secret", WEB_USERNAME="admin",
                                 DATA_DIR=str(tmp_path))
        self.db = DB(tmp_path / "web.db")
        self.strategies = self.settings.strategy_settings()
        self.runtimes = {
            key: StubRuntime(strategy, self.db)
            for key, strategy in self.strategies.items()
        }
        self.research = StubResearch()
        self.clob = None


@pytest.fixture()
def client(tmp_path):
    state = StubState(tmp_path)
    set_state(state)
    with TestClient(app) as test_client:
        yield test_client
    state.db.close()


def test_health_is_public(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "BTC:15m" in body["strategies"]
    assert "ETH:5m" in body["strategies"]
    assert len(body["strategies"]) == 14


def test_status_requires_login_when_password_set(client):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin",
                                                "password": "nope"}).status_code == 401
    response = client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    assert response.status_code == 200
    status = client.get("/api/status")
    assert status.status_code == 200
    assert len(status.json()["strategies"]) == 14


def test_config_is_scoped_per_strategy(client):
    assert client.get("/api/config").status_code == 401
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})

    payload = client.get("/api/config?strategy=5m").json()
    assert payload["scope"] == "BTC:5m"
    assert payload["values"]["MAX_ENTRY_PRICE"] == 0.90

    response = client.put("/api/config", json={
        "strategy": "5m", "values": {"MAX_ENTRY_PRICE": 0.5}
    })
    assert response.status_code == 400

    response = client.put("/api/config", json={
        "strategy": "5m", "values": {"MAX_ENTRY_PRICE": 0.91}
    })
    assert response.status_code == 200
    assert response.json()["ok"] is True

    # 15m untouched
    fifteen = client.get("/api/config?strategy=15m").json()
    assert fifteen["values"]["MAX_ENTRY_PRICE"] == 0.96

    # A different asset is a different scope.
    eth = client.get("/api/config?strategy=ETH:15m").json()
    assert eth["asset"] == "ETH"
    assert eth["values"]["MAX_ENTRY_PRICE"] == 0.92
    assert eth["overrides"] == {}


def test_strategy_matrix_and_enable_toggle(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    matrix = client.get("/api/strategies").json()
    assert matrix["assets"] == ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
    assert len(matrix["strategies"]) == 14
    assert matrix["specs"]["ENTRY_WINDOW_SEC"]["max"] == 600
    assert matrix["specs"]["ENTRY_NOTIONAL_USD_PAPER"]["step"] == 1
    eth = next(s for s in matrix["strategies"] if s["key"] == "ETH:15m")
    assert eth["enabled"] is False
    assert eth["recommended"]["threshold"] == 0.84

    response = client.put("/api/config", json={
        "strategy": "ETH:15m", "values": {"ENABLED": True}
    })
    assert response.status_code == 200
    status = {s["app"]["key"]: s for s in client.get("/api/status").json()["strategies"]}
    assert status["ETH:15m"]["app"]["enabled"] is True
    assert status["BTC:15m"]["app"]["enabled"] is True


def test_shared_config_applies_to_every_strategy(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    shared = client.get("/api/config/shared").json()
    assert shared["scope"] == "shared"
    assert "EXEC_SLIPPAGE" in shared["specs"]
    assert "DAILY_LOSS_LIMIT_USD" in shared["specs"]
    assert "ENTRY_NOTIONAL_USD_PAPER" in shared["specs"]
    assert "ENTRY_NOTIONAL_USD_LIVE" in shared["specs"]
    assert "ENTRY_WINDOW_SEC" not in shared["specs"]

    response = client.put("/api/config/shared", json={
        "values": {
            "EXEC_SLIPPAGE": 0.02,
            "DAILY_LOSS_LIMIT_USD": 25,
            "ENTRY_NOTIONAL_USD_PAPER": 12,
            "ENTRY_NOTIONAL_USD_LIVE": 6,
        },
    })
    assert response.status_code == 200
    assert response.json()["ok"] is True

    strategies = client.get("/api/status").json()["strategies"]
    assert len(strategies) == 14
    for item in strategies:
        assert item["params"]["exec_slippage"] == 0.02
        assert item["risk"]["daily_limit"] == 25
        assert item["params"]["entry_notional_paper"] == 12
        assert item["params"]["entry_notional_live"] == 6

    response = client.put("/api/config/shared", json={
        "values": {"TAKER_FEE_RATE": 0.9},
    })
    assert response.status_code == 400


def test_killswitch_is_per_strategy(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    response = client.post("/api/control/killswitch",
                           json={"strategy": "5m", "reason": "test"})
    assert response.status_code == 200
    status = {s["app"]["key"]: s for s in client.get("/api/status").json()["strategies"]}
    assert status["BTC:5m"]["risk"]["killswitch"]["active"] is True
    assert status["BTC:15m"]["risk"]["killswitch"]["active"] is False
    assert client.post("/api/control/release",
                       json={"strategy": "5m"}).status_code == 200


def test_pages_render(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "登录" in response.text
    assert 'value="admin"' in response.text
    assert "凭证来自" not in response.text

    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    for path, marker in (
        ("/", "多币种尾盘总览"),
        ("/trades", "交易记录"),
        ("/config", "参数与风控"),
        ("/research", "参数 Agent"),
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert marker in page.text, path
        assert "/static/app.js" in page.text
        assert "{{NAV}}" not in page.text
        if path == "/config":
            assert 'id="strategy-table"' in page.text
            assert 'id="ctrl-strategy"' in page.text
            assert 'id="shared-fields"' in page.text
            assert "策略列表" in page.text
            assert "共有参数" in page.text
            assert "关于这些参数" not in page.text
        if path == "/":
            assert 'id="strategy-list"' in page.text
            assert 'id="window-15m-remaining"' in page.text
            assert 'id="window-5m-remaining"' in page.text
            assert "策略说明" not in page.text


def test_all_pages_require_login(client):
    # No session yet: every page bounces to /login, even / and /trades.
    for path in ("/", "/trades", "/config", "/research"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in (302, 307), path
        assert "/login" in response.headers.get("location", ""), path
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/trades").status_code == 401

    # A static asset must stay reachable so the login page can render.
    assert client.get("/static/style.css").status_code == 200


def test_mode_endpoint_switches_strategy(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    response = client.post("/api/control/mode",
                           json={"strategy": "5m", "mode": "live"})
    assert response.status_code == 200
    assert response.json()["mode"] == "live"
    status = {s["app"]["key"]: s for s in client.get("/api/status").json()["strategies"]}
    assert status["BTC:5m"]["app"]["mode"] == "live"
    assert status["BTC:15m"]["app"]["mode"] == "paper"


def test_empty_password_closes_dashboard(tmp_path):
    state = StubState(tmp_path)
    state.settings.WEB_PASSWORD = ""
    set_state(state)
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/health").status_code == 200
    state.db.close()
