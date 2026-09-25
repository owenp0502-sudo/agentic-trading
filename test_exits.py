"""
test_exits.py — Deterministic unit tests for the exit strategy math and
response parsers. Same payload in → same verdict out. No network.

Run:  python -m pytest test_exits.py -q   (or python test_exits.py)
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import config
import exits

NY = ZoneInfo(config.TIMEZONE)


# ---------------------------------------------------------------------------
# Bracket levels
# ---------------------------------------------------------------------------
def test_bracket_levels_long():
    stop, target = exits.bracket_levels("BUY", 100.0)
    assert stop == 99.5          # 0.5% below cost
    assert target == 101.0       # 1.0% above cost (2R)


def test_bracket_levels_short():
    stop, target = exits.bracket_levels("SELL", 100.0)
    assert stop == 100.5
    assert target == 99.0


def test_bracket_levels_rejects_bad_cost():
    try:
        exits.bracket_levels("BUY", 0.0)
        assert False, "should raise on zero cost"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Hit decisions
# ---------------------------------------------------------------------------
def test_stop_hit_long():
    assert exits.stop_hit("BUY", 100.0, 99.4) is True
    assert exits.stop_hit("BUY", 100.0, 99.6) is False


def test_target_hit_long():
    assert exits.target_hit("BUY", 100.0, 101.1) is True
    assert exits.target_hit("BUY", 100.0, 100.9) is False


def test_stop_hit_short_mirrored():
    assert exits.stop_hit("SELL", 100.0, 100.6) is True
    assert exits.stop_hit("SELL", 100.0, 100.4) is False


def test_target_hit_short_mirrored():
    assert exits.target_hit("SELL", 100.0, 98.9) is True
    assert exits.target_hit("SELL", 100.0, 99.1) is False


# ---------------------------------------------------------------------------
# Flatten time gate
# ---------------------------------------------------------------------------
def test_flatten_at_and_after_1100():
    assert exits.should_flatten(datetime(2026, 9, 25, 11, 0, tzinfo=NY)) is True
    assert exits.should_flatten(datetime(2026, 9, 25, 11, 5, tzinfo=NY)) is True
    assert exits.should_flatten(datetime(2026, 9, 25, 15, 30, tzinfo=NY)) is True


def test_no_flatten_before_1100():
    assert exits.should_flatten(datetime(2026, 9, 25, 9, 30, tzinfo=NY)) is False
    assert exits.should_flatten(datetime(2026, 9, 25, 10, 59, tzinfo=NY)) is False


def test_flatten_uses_ny_time_not_utc():
    # 14:00 UTC == 10:00 ET (September, EDT) → must NOT flatten
    utc_then = datetime(2026, 9, 25, 14, 0, tzinfo=ZoneInfo("UTC"))
    assert exits.should_flatten(utc_then) is False
    # 16:00 UTC == 12:00 ET → must flatten
    utc_late = datetime(2026, 9, 25, 16, 0, tzinfo=ZoneInfo("UTC"))
    assert exits.should_flatten(utc_late) is True


# ---------------------------------------------------------------------------
# Account parser
# ---------------------------------------------------------------------------
ACCOUNTS_JSON = """{
  "accounts": [{
    "account_number": "AGNT123456",
    "agentic_allowed": true,
    "portfolio_cash": 5000.0,
    "type": "cash"
  }]
}"""

ACCOUNTS_NON_AGENTIC = """{
  "accounts": [{
    "account_number": "NORMAL999",
    "agentic_allowed": false
  }]
}"""


def test_parse_account_number():
    assert exits.parse_account_number(ACCOUNTS_JSON) == "AGNT123456"


def test_parse_account_number_skips_non_agentic():
    assert exits.parse_account_number(ACCOUNTS_NON_AGENTIC) is None


def test_parse_account_number_garbage():
    assert exits.parse_account_number("oops not json") is None
    assert exits.parse_account_number("") is None


# ---------------------------------------------------------------------------
# Positions parser
# ---------------------------------------------------------------------------
POSITIONS_JSON = """{
  "positions": [
    {"symbol": "SPY", "quantity": "10.5", "average_buy_price": "100.25"},
    {"symbol": "AMD", "quantity": "0", "average_buy_price": "90.00"},
    {"quantity": "3", "average_buy_price": "50"}
  ]
}"""


def test_parse_positions_open_only():
    out = exits.parse_positions(POSITIONS_JSON)
    symbols = [p["symbol"] for p in out]
    assert symbols == ["SPY"]          # zero-qty and symbol-less dropped
    assert out[0]["quantity"] == 10.5
    assert out[0]["avg_cost"] == 100.25


def test_parse_positions_empty():
    assert exits.parse_positions('{"positions": []}') == []
    assert exits.parse_positions("") == []


# ---------------------------------------------------------------------------
# Quote parser
# ---------------------------------------------------------------------------
QUOTES_JSON = """{
  "SPY": {"symbol": "SPY", "last_trade_price": "100.55"},
  "AMD": {"symbol": "AMD", "last_trade_price": "88.10"}
}"""


def test_parse_last_price():
    assert exits.parse_last_price(QUOTES_JSON, "SPY") == 100.55
    assert exits.parse_last_price(QUOTES_JSON, "amd") == 88.10
    assert exits.parse_last_price("{}", "SPY") is None
    assert exits.parse_last_price("garbage", "SPY") is None


# ---------------------------------------------------------------------------
# Open-orders parser
# ---------------------------------------------------------------------------
ORDERS_JSON = """{
  "orders": [
    {"order_id": "abc-123", "state": "confirmed", "symbol": "SPY",
     "side": "sell", "type": "stop_market"},
    {"order_id": "def-456", "state": "filled", "symbol": "SPY",
     "side": "sell", "type": "market"},
    {"order_id": "ghi-789", "state": "cancelled", "symbol": "SPY",
     "side": "sell", "type": "limit"}
  ]
}"""


def test_parse_open_orders_resting_only():
    out = exits.parse_open_orders(ORDERS_JSON)
    assert len(out) == 1
    assert out[0]["order_id"] == "abc-123"
    assert out[0]["type"] == "stop_market"


def test_parse_open_orders_empty():
    assert exits.parse_open_orders('{"orders": []}') == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
