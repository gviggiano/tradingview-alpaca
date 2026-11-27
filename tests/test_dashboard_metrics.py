"""Dashboard ordering, pagination, and risk metrics.

The Python helpers are the API payload. The browser repeats them in
static/ledger_metrics.js so source and period filters stay local. These tests
lock both implementations to the same results.
"""

import json
import subprocess
from pathlib import Path

import pytest
from flask import Flask, jsonify, render_template

import dashboard


ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "static" / "ledger_metrics.js"
TEMPLATE = ROOT / "templates" / "dashboard.html"


def trade(
    closed_at,
    pnl,
    *,
    pnl_pct=None,
    source="live",
    symbol="AAPL",
    outcome=None,
    opened_at=None,
):
    if outcome is None:
        outcome = "tp" if pnl > 0 else "sl" if pnl < 0 else "tp"
    if pnl_pct is None:
        pnl_pct = pnl
    return {
        "symbol": symbol,
        "side": "buy",
        "qty": 1,
        "entryPrice": 100,
        "exitPrice": 100 + pnl,
        "outcome": outcome,
        "pnl": pnl,
        "pnlPct": pnl_pct,
        "openedAt": opened_at or closed_at,
        "closedAt": closed_at,
        "source": source,
    }


def position(entry, qty, side="buy", sl=None, filled_at="2026-01-01T00:00:00", source="live"):
    return {
        "symbol": "AAPL",
        "side": side,
        "qty": qty,
        "entryPrice": entry,
        "filledAt": filled_at,
        "tp": None,
        "sl": sl,
        "source": source,
    }


def dated_trades(count, start_day=1):
    return [
        trade(f"2026-01-{day:02d}T00:00:00", 1, symbol=f"T{day}")
        for day in range(start_day, start_day + count)
    ]


def js_call(fn, *args):
    payload = json.dumps(list(args))
    name = json.dumps(fn)
    script = f"""
const m = require({json.dumps(str(JS_PATH))});
const args = {payload};
const result = {name} === "TRADES_PAGE_SIZE" ? m.TRADES_PAGE_SIZE : m[{name}](...args);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def assert_close(actual, expected):
    if expected is None or actual is None:
        assert actual is None and expected is None
        return
    if isinstance(expected, bool):
        assert actual is expected
        return
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        assert actual == pytest.approx(expected, rel=1e-9, abs=1e-9)
        return
    assert actual == expected


def assert_risk_match(py_report, js_report):
    mapping = {
        "count": "count",
        "max_drawdown": "maxDrawdown",
        "max_drawdown_pct": "maxDrawdownPct",
        "current_drawdown": "currentDrawdown",
        "current_drawdown_pct": "currentDrawdownPct",
        "peak_equity": "peakEquity",
        "equity": "equity",
        "max_losing_streak": "maxLosingStreak",
        "payoff_ratio": "payoffRatio",
        "payoff_infinite": "payoffInfinite",
        "expectancy": "expectancy",
        "expectancy_pct": "expectancyPct",
        "avg_win": "avgWin",
        "avg_loss": "avgLoss",
    }
    assert set(js_report) == set(mapping.values())
    for py_key, js_key in mapping.items():
        assert_close(js_report[js_key], py_report[py_key])


def assert_page_match(py_page, js_page):
    assert js_page["shown"] == py_page["shown"]
    assert js_page["total"] == py_page["total"]
    assert js_page["remaining"] == py_page["remaining"]
    assert js_page["showMore"] is py_page["show_more"]
    assert js_page["nextCount"] == py_page["next_count"]
    py_rows = [
        {
            "closedAt": row["closedAt"],
            "number": row["number"],
            "pnl": row["pnl"],
            "source": row["source"],
            "symbol": row["symbol"],
        }
        for row in py_page["rows"]
    ]
    js_rows = [
        {
            "closedAt": row["closedAt"],
            "number": row["number"],
            "pnl": row["pnl"],
            "source": row["source"],
            "symbol": row["symbol"],
        }
        for row in js_page["rows"]
    ]
    assert js_rows == py_rows


class TestOrdering:
    def test_trades_display_newest_first_and_equity_stays_chronological(self):
        live = [
            trade("2026-01-02T00:00:00", 10, source="live", symbol="AAPL"),
            trade("2026-01-05T00:00:00", -4, source="live", symbol="AAPL"),
        ]
        sim = [
            trade("2026-01-01T00:00:00", 3, source="sim", symbol="SOXL"),
            trade("2026-01-04T00:00:00", 8, pnl_pct=1.5, source="sim", symbol="SOXL"),
        ]
        payload = dashboard.assemble_dashboard_payload(live, [], sim, [], [])
        closed_at = [item["closedAt"] for item in payload["closed"]]
        assert closed_at == [
            "2026-01-05T00:00:00",
            "2026-01-04T00:00:00",
            "2026-01-02T00:00:00",
            "2026-01-01T00:00:00",
        ]

        series = dashboard.equity_series(payload["closed"])
        assert [item["closedAt"] for item in series["trades"]] == [
            "2026-01-01T00:00:00",
            "2026-01-02T00:00:00",
            "2026-01-04T00:00:00",
            "2026-01-05T00:00:00",
        ]
        assert series["points"] == pytest.approx([3, 13, 21, 17])

        mixed = dashboard.equity_series(list(reversed(payload["closed"])), use_pct=True)
        assert [item["closedAt"] for item in mixed["trades"]] == [
            item["closedAt"] for item in series["trades"]
        ]
        assert mixed["points"][0] == pytest.approx(3)
        js_mixed = js_call("equitySeries", payload["closed"], True)
        assert [item["closedAt"] for item in js_mixed["trades"]] == [
            item["closedAt"] for item in mixed["trades"]
        ]
        assert js_mixed["points"] == pytest.approx(mixed["points"])

        js_series = js_call("equitySeries", payload["closed"], False)
        assert [item["closedAt"] for item in js_series["trades"]] == [
            item["closedAt"] for item in series["trades"]
        ]
        assert js_series["points"] == pytest.approx(series["points"])

    def test_missing_close_time_sorts_as_oldest(self):
        trades = [
            trade("2026-03-02T00:00:00", 1),
            trade(None, -1),
        ]
        newest = dashboard.sort_closed_newest_first(trades)
        assert newest[0]["closedAt"] == "2026-03-02T00:00:00"
        assert newest[1]["closedAt"] is None
        chrono = dashboard.sort_closed_chronological(trades)
        assert chrono[0]["closedAt"] is None

    def test_open_positions_newest_first_with_risk_fields(self):
        older = position(100, 10, sl=90, filled_at="2026-02-01T00:00:00")
        newer = position(50, 2, side="sell", sl=55, filled_at="2026-02-05T00:00:00", source="sim")
        payload = dashboard.assemble_dashboard_payload([], [older, newer], [], [], [])
        assert [item["filledAt"] for item in payload["open"]] == [
            "2026-02-05T00:00:00",
            "2026-02-01T00:00:00",
        ]
        assert payload["open"][1]["notional"] == 1000
        assert payload["open"][1]["riskToSl"] == 100
        assert payload["open"][1]["riskToSlPct"] == pytest.approx(10)
        assert payload["open"][0]["riskToSl"] == 10

    def test_overlapping_sim_still_dropped_before_display_sort(self):
        live = [trade("2026-06-10T00:00:00", 5, source="live", symbol="MSFT")]
        sim = [
            trade("2026-06-01T00:00:00", 2, source="sim", symbol="MSFT"),
            trade("2026-06-11T00:00:00", 9, source="sim", symbol="MSFT"),
        ]
        payload = dashboard.assemble_dashboard_payload(live, [], sim, [], [])
        assert [item["closedAt"] for item in payload["closed"]] == [
            "2026-06-10T00:00:00",
            "2026-06-01T00:00:00",
        ]


class TestPagination:
    def test_more_than_ten_starts_at_ten_and_show_more_reveals_the_rest(self):
        trades = dated_trades(25)
        shown = dashboard.next_shown(0, len(trades), "init")
        first = dashboard.present_trades(trades, shown)
        assert first["shown"] == 10
        assert first["show_more"] is True
        assert first["remaining"] == 15
        assert first["next_count"] == 10
        assert [row["closedAt"] for row in first["rows"]] == [
            f"2026-01-{day:02d}T00:00:00" for day in range(25, 15, -1)
        ]
        assert first["rows"][0]["number"] == 25
        assert first["rows"][-1]["number"] == 16
        assert_page_match(first, js_call("presentTrades", trades, shown))

        shown = dashboard.next_shown(shown, len(trades), "more")
        second = dashboard.present_trades(trades, shown)
        assert second["shown"] == 20
        assert second["show_more"] is True
        assert second["rows"][10]["closedAt"] == "2026-01-15T00:00:00"
        assert second["rows"][10]["closedAt"] not in {
            row["closedAt"] for row in first["rows"]
        }
        assert_page_match(second, js_call("presentTrades", trades, shown))

        shown = dashboard.next_shown(shown, len(trades), "more")
        third = dashboard.present_trades(trades, shown)
        assert third["shown"] == 25
        assert third["show_more"] is False
        assert third["remaining"] == 0
        assert [row["number"] for row in third["rows"]] == list(range(25, 0, -1))

        shown = dashboard.next_shown(10, len(trades), "all")
        everything = dashboard.present_trades(trades, shown)
        assert everything["shown"] == 25
        assert everything["show_more"] is False

    def test_ten_or_fewer_shows_every_trade_and_hides_the_button(self):
        for count in (0, 1, 10):
            trades = dated_trades(count)
            shown = dashboard.next_shown(0, count, "init")
            page = dashboard.present_trades(trades, shown)
            assert page["shown"] == count
            assert page["show_more"] is False
            assert page["remaining"] == 0
            assert len(page["rows"]) == count
            assert_page_match(page, js_call("presentTrades", trades, shown))

    def test_eleven_trades_show_more_then_hide(self):
        trades = dated_trades(11)
        shown = dashboard.next_shown(0, 11, "init")
        page = dashboard.present_trades(trades, shown)
        assert page["show_more"] is True
        assert page["next_count"] == 1
        shown = dashboard.next_shown(shown, 11, "more")
        page = dashboard.present_trades(trades, shown)
        assert page["shown"] == 11
        assert page["show_more"] is False

    def test_filter_resets_the_window_to_ten(self):
        assert dashboard.next_shown(50, 80, "filter") == 10
        assert dashboard.next_shown(50, 4, "filter") == 4
        assert dashboard.next_shown(50, 80, "reset") == 10
        assert js_call("nextShown", 50, 80, "filter") == 10
        assert js_call("nextShown", 50, 4, "filter") == 4
        assert js_call("nextShown", 10, 80, "all") == 80
        assert js_call("nextShown", 10, 25, "more") == 20

    def test_page_size_matches_the_browser_module(self):
        assert dashboard.TRADES_PAGE_SIZE == 10
        assert js_call("TRADES_PAGE_SIZE") == 10


class TestRiskMetrics:
    def test_empty_book(self):
        report = dashboard.risk_report([])
        assert report["count"] == 0
        assert report["max_drawdown"] == 0
        assert report["max_drawdown_pct"] is None
        assert report["current_drawdown"] == 0
        assert report["current_drawdown_pct"] is None
        assert report["max_losing_streak"] == 0
        assert report["payoff_ratio"] is None
        assert report["payoff_infinite"] is False
        assert report["expectancy"] is None
        assert report["expectancy_pct"] is None
        assert_risk_match(report, js_call("riskReport", []))

    def test_all_wins(self):
        trades = [
            trade("2026-04-01T00:00:00", 10, pnl_pct=1),
            trade("2026-04-02T00:00:00", 30, pnl_pct=3),
        ]
        report = dashboard.risk_report(trades)
        assert report["max_drawdown"] == 0
        assert report["max_drawdown_pct"] == 0
        assert report["current_drawdown"] == 0
        assert report["current_drawdown_pct"] == 0
        assert report["max_losing_streak"] == 0
        assert report["payoff_infinite"] is True
        assert report["payoff_ratio"] is None
        assert report["avg_win"] == pytest.approx(20)
        assert report["avg_loss"] is None
        assert report["expectancy"] == pytest.approx(20)
        assert report["expectancy_pct"] == pytest.approx(2)
        assert report["equity"] == pytest.approx(40)
        assert_risk_match(report, js_call("riskReport", trades))

    def test_all_losses(self):
        trades = [
            trade("2026-03-02T00:00:00", -30, pnl_pct=-3),
            trade("2026-03-01T00:00:00", -10, pnl_pct=-1),
        ]
        report = dashboard.risk_report(trades)
        assert report["max_drawdown"] == pytest.approx(40)
        assert report["max_drawdown_pct"] is None
        assert report["current_drawdown"] == pytest.approx(40)
        assert report["current_drawdown_pct"] is None
        assert report["peak_equity"] == 0
        assert report["max_losing_streak"] == 2
        assert report["payoff_ratio"] == 0
        assert report["payoff_infinite"] is False
        assert report["avg_win"] is None
        assert report["avg_loss"] == pytest.approx(20)
        assert report["expectancy"] == pytest.approx(-20)
        assert report["expectancy_pct"] == pytest.approx(-2)
        assert_risk_match(report, js_call("riskReport", trades))

    def test_mixed_path_drawdown_streak_and_payoff(self):
        trades = [
            trade("2026-05-03T00:00:00", -40, pnl_pct=-4),
            trade("2026-05-01T00:00:00", 100, pnl_pct=10),
            trade("2026-05-02T00:00:00", 50, pnl_pct=5),
            trade("2026-05-04T00:00:00", -30, pnl_pct=-3),
            trade("2026-05-05T00:00:00", 10, pnl_pct=1),
        ]
        report = dashboard.risk_report(trades)
        assert report["equity"] == pytest.approx(90)
        assert report["peak_equity"] == pytest.approx(150)
        assert report["max_drawdown"] == pytest.approx(70)
        assert report["max_drawdown_pct"] == pytest.approx(70 / 150 * 100)
        assert report["current_drawdown"] == pytest.approx(60)
        assert report["current_drawdown_pct"] == pytest.approx(40)
        assert report["max_losing_streak"] == 2
        assert report["avg_win"] == pytest.approx((100 + 50 + 10) / 3)
        assert report["avg_loss"] == pytest.approx(35)
        assert report["payoff_ratio"] == pytest.approx(report["avg_win"] / 35)
        assert report["expectancy"] == pytest.approx(90 / 5)
        assert report["expectancy_pct"] == pytest.approx((10 + 5 - 4 - 3 + 1) / 5)
        assert_risk_match(report, js_call("riskReport", trades))

    def test_recovery_clears_current_drawdown_but_keeps_the_max(self):
        trades = [
            trade("2026-07-01T00:00:00", 100),
            trade("2026-07-02T00:00:00", -40),
            trade("2026-07-03T00:00:00", 50),
        ]
        report = dashboard.risk_report(trades)
        assert report["max_drawdown"] == pytest.approx(40)
        assert report["max_drawdown_pct"] == pytest.approx(40)
        assert report["current_drawdown"] == 0
        assert report["current_drawdown_pct"] == 0
        assert report["equity"] == pytest.approx(110)

    def test_stop_outcome_extends_the_losing_streak_even_if_pnl_is_positive(self):
        trades = [
            trade("2026-05-01T00:00:00", 5, outcome="sl"),
            trade("2026-05-02T00:00:00", -2, outcome="sl"),
            trade("2026-05-03T00:00:00", 4, outcome="tp"),
        ]
        report = dashboard.risk_report(trades)
        assert report["max_losing_streak"] == 2
        assert report["payoff_ratio"] == pytest.approx(4.5 / 2)
        assert report["expectancy"] == pytest.approx((5 - 2 + 4) / 3)
        assert_risk_match(report, js_call("riskReport", trades))

    def test_live_and_sim_risk_are_not_blended(self):
        sim = [
            trade("2026-02-01T00:00:00", -50, source="sim"),
            trade("2026-02-02T00:00:00", -50, source="sim"),
        ]
        live = [trade("2026-02-03T00:00:00", 20, pnl_pct=2, source="live")]
        mixed = sim + live
        by_source = dashboard.risk_by_source(mixed)
        assert by_source["sim"]["max_losing_streak"] == 2
        assert by_source["sim"]["max_drawdown"] == pytest.approx(100)
        assert by_source["sim"]["expectancy"] == pytest.approx(-50)
        assert by_source["live"]["max_losing_streak"] == 0
        assert by_source["live"]["max_drawdown"] == 0
        assert by_source["live"]["payoff_infinite"] is True
        assert by_source["live"]["expectancy"] == pytest.approx(20)

        blended = dashboard.risk_report(mixed)
        assert blended["max_drawdown"] != by_source["live"]["max_drawdown"]

        js_by_source = js_call("riskBySource", mixed)
        assert_risk_match(by_source["sim"], js_by_source["sim"])
        assert_risk_match(by_source["live"], js_by_source["live"])

        health = dashboard.source_health(mixed)
        assert health["sim"]["count"] == 2
        assert health["sim"]["pnl"] == pytest.approx(-100)
        assert health["live"]["count"] == 1
        assert health["live"]["pnl"] == pytest.approx(20)
        assert health["last_closed_at"] == "2026-02-03T00:00:00"
        assert health["sim"]["last_closed_at"] == "2026-02-02T00:00:00"

        js_health = js_call("sourceHealth", mixed)
        assert js_health["sim"]["count"] == 2
        assert js_health["sim"]["pnl"] == pytest.approx(-100)
        assert js_health["live"]["pnl"] == pytest.approx(20)
        assert js_health["lastClosedAt"] == "2026-02-03T00:00:00"

    def test_missing_source_counts_as_live(self):
        unmarked = trade("2026-08-01T00:00:00", 7)
        unmarked.pop("source")
        health = dashboard.source_health([unmarked])
        assert health["live"]["count"] == 1
        assert health["sim"]["count"] == 0
        assert health["last_closed_at"] == "2026-08-01T00:00:00"


class TestOpenRisk:
    def test_buy_and_sell_formulas(self):
        buy = dashboard.open_position_risk(
            {"entryPrice": 100, "qty": 10, "side": "buy", "sl": 95}
        )
        assert buy["notional"] == 1000
        assert buy["risk"] == pytest.approx(50)
        assert buy["risk_pct"] == pytest.approx(5)

        sell = dashboard.open_position_risk(
            {"entryPrice": 100, "qty": 2, "side": "sell", "sl": 110}
        )
        assert sell["notional"] == 200
        assert sell["risk"] == pytest.approx(20)
        assert sell["risk_pct"] == pytest.approx(10)

        short = dashboard.open_position_risk(
            {"entryPrice": "80", "qty": "1", "side": "short", "sl": "90"}
        )
        assert short["risk"] == pytest.approx(10)

        locked = dashboard.open_position_risk(
            {"entryPrice": 100, "qty": 10, "side": "buy", "sl": 105}
        )
        assert locked["risk"] == pytest.approx(-50)
        assert locked["risk_pct"] == pytest.approx(-5)

        for case in (
            {"entryPrice": 100, "qty": 10, "side": "buy", "sl": 95},
            {"entryPrice": 100, "qty": 2, "side": "sell", "sl": 110},
            {"entryPrice": "80", "qty": "1", "side": "short", "sl": "90"},
            {"entryPrice": 100, "qty": 10, "side": "buy", "sl": 105},
        ):
            js_row = js_call("openPositionRisk", case)
            py_row = dashboard.open_position_risk(case)
            assert js_row["notional"] == pytest.approx(py_row["notional"])
            assert_close(js_row["risk"], py_row["risk"])
            assert_close(js_row["riskPct"], py_row["risk_pct"])

    def test_missing_stop_and_totals(self):
        missing = dashboard.open_position_risk(
            {"entryPrice": "100", "qty": "2", "side": "buy", "sl": "—"}
        )
        assert missing["notional"] == 200
        assert missing["risk"] is None
        assert missing["risk_pct"] is None

        positions = [
            {"entryPrice": 100, "qty": 10, "side": "buy", "sl": 95},
            {"entryPrice": 100, "qty": 2, "side": "sell", "sl": 110},
            {"entryPrice": 50, "qty": 4, "side": "buy", "sl": None},
        ]
        summary = dashboard.summarize_open_positions(positions)
        assert summary["notional"] == pytest.approx(1400)
        assert summary["risk"] == pytest.approx(70)
        assert summary["with_sl"] == 2
        assert summary["positions"] == 3

        empty = dashboard.summarize_open_positions([])
        assert empty["positions"] == 0
        assert empty["risk"] is None
        assert empty["notional"] == 0

        no_stops = dashboard.summarize_open_positions([
            {"entryPrice": 10, "qty": 3, "side": "buy", "sl": None},
        ])
        assert no_stops["notional"] == pytest.approx(30)
        assert no_stops["risk"] is None
        assert no_stops["with_sl"] == 0

        js_summary = js_call("summarizeOpenRisk", positions)
        assert js_summary["notional"] == pytest.approx(summary["notional"])
        assert js_summary["risk"] == pytest.approx(summary["risk"])
        assert js_summary["withSl"] == 2
        assert js_summary["positions"] == 3
        js_empty = js_call("summarizeOpenRisk", [])
        assert js_empty["risk"] is None


class TestPayloadAndTemplate:
    def test_analytics_json_has_no_infinity(self):
        trades = [trade("2026-09-01T00:00:00", 12, source="live")]
        payload = dashboard.assemble_dashboard_payload(trades, [], [], [], [])
        app = Flask(__name__)
        with app.app_context():
            encoded = jsonify(payload).get_data(as_text=True)
        parsed = json.loads(encoded)
        live = parsed["analytics"]["bySource"]["live"]
        assert live["payoffInfinite"] is True
        assert live["payoffRatio"] is None
        assert parsed["pageSize"] == 10
        assert parsed["analytics"]["health"]["live"]["count"] == 1
        assert parsed["analytics"]["health"]["sim"]["count"] == 0
        assert "Infinity" not in encoded

    def test_template_wires_sort_pagination_and_risk(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert "slice(-50)" not in html
        assert "ledger_metrics.js" in html
        assert 'id="showMoreBtn"' in html
        assert 'id="showAllBtn"' in html
        assert 'id="tradesMore"' in html
        assert 'id="tradesBody"' in html
        assert 'id="tradesCards"' in html
        assert 'id="healthStrip"' in html
        assert 'id="statMaxDd"' in html
        assert 'id="statCurDd"' in html
        assert 'id="statStreak"' in html
        assert 'id="statPayoff"' in html
        assert 'id="statExpectancy"' in html
        assert 'id="openRiskSummary"' in html
        assert "Nozionale" in html
        assert "Rischio SL" in html
        assert "equitySeries(closed, mixed)" in html
        assert "presentTrades(closed, tradesVisible)" in html
        assert "riskBySource(closed)" in html
        assert "nextShown(" in html
        assert "'filter'" in html
        assert "'more'" in html
        assert "'all'" in html
        assert "'init'" in html
        assert "for(let i = count - 1; i >= 0; i--)" in html
        assert "sortOpenNewestFirst" in html

        script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        completed = subprocess.run(
            ["node", "--check", "-"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
        )
        # The inline script is classic browser JS. `node --check` still parses it.
        # A leading assignment to LedgerMetrics is valid syntax either way.
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)

    def test_dashboard_template_renders(self):
        app = Flask(
            __name__,
            template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"),
        )

        @app.route("/dashboard")
        def dashboard_page():
            return render_template("dashboard.html")

        client = app.test_client()
        page = client.get("/dashboard")
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert "/static/ledger_metrics.js" in body
        assert 'id="showMoreBtn"' in body
        script = client.get("/static/ledger_metrics.js")
        assert script.status_code == 200
        assert b"presentTrades" in script.data
