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


def test_live_mode_requires_credentials():
    settings = Settings(TRADING_MODE="live", API_KEY="", WALLET_PRIVATE_KEY="",
                        MAX_ENTRY_PRICE=0.94)
    assert any("实盘" in message for message in settings.validation_errors())


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
        self.risk = RiskManager(db, settings, period=settings.PERIOD, mode="paper")
        self.broker = StubBroker()
        self._started_at = time.time()
        self._last_balance_ts = time.time()

    def status(self, **kwargs):
        return {
            "app": {"mode": "paper", "paper": True, "uptime_s": 1.0,
                    "period": self.settings.PERIOD, "asset": "BTC"},
            "params": {}, "window": None, "strategy": None, "quote": None,
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
        self.strategies = {"15m": self.settings.for_period("15m"),
                           "5m": self.settings.for_period("5m")}
        self.runtimes = {
            "15m": StubRuntime(self.strategies["15m"], self.db),
            "5m": StubRuntime(self.strategies["5m"], self.db),
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
    assert set(body["strategies"]) == {"15m", "5m"}


def test_status_requires_login_when_password_set(client):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin",
                                                "password": "nope"}).status_code == 401
    response = client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    assert response.status_code == 200
    status = client.get("/api/status")
    assert status.status_code == 200
    assert len(status.json()["strategies"]) == 2


def test_config_is_scoped_per_strategy(client):
    assert client.get("/api/config").status_code == 401
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})

    payload = client.get("/api/config?strategy=5m").json()
    assert payload["scope"] == "5m"
    assert payload["values"]["MAX_ENTRY_PRICE"] == 0.92

    response = client.put("/api/config", json={
        "strategy": "5m", "values": {"MAX_ENTRY_PRICE": 0.5}
    })
    assert response.status_code == 400

    response = client.put("/api/config", json={
        "strategy": "5m", "values": {"MAX_ENTRY_PRICE": 0.93}
    })
    assert response.status_code == 200
    assert response.json()["ok"] is True

    # 15m untouched
    fifteen = client.get("/api/config?strategy=15m").json()
    assert fifteen["values"]["MAX_ENTRY_PRICE"] == 0.94


def test_killswitch_is_per_strategy(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    response = client.post("/api/control/killswitch",
                           json={"strategy": "5m", "reason": "test"})
    assert response.status_code == 200
    status = {s["app"]["period"]: s for s in client.get("/api/status").json()["strategies"]}
    assert status["5m"]["risk"]["killswitch"]["active"] is True
    assert status["15m"]["risk"]["killswitch"]["active"] is False
    assert client.post("/api/control/release",
                       json={"strategy": "5m"}).status_code == 200


def test_pages_render(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "登录" in response.text

    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    for path, marker in (
        ("/", "双策略总览"),
        ("/trades", "交易记录"),
        ("/config", "参数与风控"),
        ("/research", "参数 Agent"),
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert marker in page.text, path
        assert "/static/app.js" in page.text
        assert "{{NAV}}" not in page.text
