from collections import defaultdict
from datetime import datetime
from pathlib import Path
import csv
import os

from flask import jsonify, render_template
import alpaca_trade_api as tradeapi


SIMULATION_DIR = Path(__file__).resolve().parent / "simulation"

CSV_DT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_number(value):
    if value is None:
        return None

    text = str(value).strip().replace(",", "")
    if text in ("", "—", "-", "Open"):
        return None

    try:
        return float(text)
    except ValueError:
        return None


def _parse_csv_datetime(value):
    if value is None:
        return None

    text = str(value).strip()
    if text in ("", "Open"):
        return None

    for fmt in CSV_DT_FORMATS:
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue

    return text


def _to_iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _symbol_from_csv_path(path):
    return path.stem.split("_")[0].upper()


def _latest_csv_per_symbol(folder):
    latest = {}

    if not folder.is_dir():
        return latest

    for path in folder.glob("*.csv"):
        symbol = _symbol_from_csv_path(path)
        previous = latest.get(symbol)
        if previous is None or path.stat().st_mtime > previous.stat().st_mtime:
            latest[symbol] = path

    return latest


def _parse_simulation_csv(path):
    symbol = _symbol_from_csv_path(path)
    groups = defaultdict(lambda: {"entries": [], "exits": [], "side": "buy"})

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)

        for row in reader:
            trade_no = (row.get("Trade number") or "").strip()
            if not trade_no:
                continue

            kind = (row.get("Type") or "").strip().lower()
            if kind.startswith("entry"):
                groups[trade_no]["entries"].append(row)
                groups[trade_no]["side"] = (
                    "sell" if "short" in kind else "buy"
                )
            elif kind.startswith("exit"):
                groups[trade_no]["exits"].append(row)

    closed = []
    opened = []

    for group in groups.values():
        if not group["entries"]:
            continue

        entry = group["entries"][0]
        exit_row = group["exits"][0] if group["exits"] else None
        qty = _parse_number(entry.get("Size (qty)"))
        entry_price = _parse_number(entry.get("Price USD"))
        opened_at = _parse_csv_datetime(entry.get("Date and time"))

        if qty is None or entry_price is None:
            continue

        exit_at = (
            _parse_csv_datetime(exit_row.get("Date and time"))
            if exit_row
            else None
        )
        exit_price = (
            _parse_number(exit_row.get("Price USD"))
            if exit_row
            else None
        )

        is_open = exit_row is None or exit_at is None or exit_price is None

        if is_open:
            opened.append({
                "symbol": symbol,
                "side": group["side"],
                "qty": qty,
                "entryPrice": entry_price,
                "filledAt": opened_at,
                "tp": None,
                "sl": None,
                "source": "sim",
            })
            continue

        pnl = _parse_number(exit_row.get("Net PnL USD"))
        if pnl is None:
            if group["side"] == "buy":
                pnl = (exit_price - entry_price) * qty
            else:
                pnl = (entry_price - exit_price) * qty

        pnl_pct = _parse_number(exit_row.get("Return %"))
        if pnl_pct is None and entry_price and qty:
            pnl_pct = (pnl / (entry_price * qty)) * 100

        closed.append({
            "symbol": symbol,
            "side": group["side"],
            "qty": qty,
            "entryPrice": entry_price,
            "exitPrice": exit_price,
            "outcome": "tp" if (pnl or 0) > 0 else "sl",
            "pnl": pnl or 0,
            "pnlPct": pnl_pct or 0,
            "openedAt": opened_at,
            "closedAt": exit_at,
            "source": "sim",
        })

    return closed, opened


def _load_simulation_trades():
    closed = []
    opened = []
    files = []

    for symbol, path in _latest_csv_per_symbol(SIMULATION_DIR).items():
        file_closed, file_opened = _parse_simulation_csv(path)
        closed.extend(file_closed)
        opened.extend(file_opened)
        files.append({
            "symbol": symbol,
            "file": path.name,
            "closed": len(file_closed),
            "open": len(file_opened),
        })

    return closed, opened, files


def _first_live_date(live_closed, live_open, symbol):
    dates = []

    for trade in live_closed:
        if trade["symbol"] != symbol:
            continue
        parsed = _as_datetime(trade.get("openedAt") or trade.get("closedAt"))
        if parsed:
            dates.append(parsed.date())

    for trade in live_open:
        if trade["symbol"] != symbol:
            continue
        parsed = _as_datetime(trade.get("filledAt"))
        if parsed:
            dates.append(parsed.date())

    return min(dates) if dates else None


def _drop_overlapping_sim(sim_closed, sim_open, live_closed, live_open):
    symbols = {
        trade["symbol"]
        for trade in sim_closed + sim_open
    }

    cutoffs = {
        symbol: _first_live_date(live_closed, live_open, symbol)
        for symbol in symbols
    }

    def keep_closed(trade):
        cutoff = cutoffs.get(trade["symbol"])
        if cutoff is None:
            return True
        closed_at = _as_datetime(trade.get("closedAt"))
        return closed_at is None or closed_at.date() < cutoff

    def keep_open(trade):
        cutoff = cutoffs.get(trade["symbol"])
        if cutoff is None:
            return True
        filled_at = _as_datetime(trade.get("filledAt"))
        return filled_at is None or filled_at.date() < cutoff

    return (
        [trade for trade in sim_closed if keep_closed(trade)],
        [trade for trade in sim_open if keep_open(trade)],
    )


def _collect_live_trades(api):
    orders = api.list_orders(
        status="all",
        limit=500,
        nested=True
    )

    closed_trades = []
    open_trades = []

    for order in orders:
        if order.order_class != "bracket":
            continue

        if not order.filled_qty or not order.filled_avg_price:
            continue

        try:
            entry_qty = float(order.filled_qty)
            entry_price = float(order.filled_avg_price)
        except (TypeError, ValueError):
            continue

        legs = order.legs or []

        take_profit = next(
            (leg for leg in legs if leg.type == "limit"),
            None
        )

        stop_loss = next(
            (
                leg for leg in legs
                if leg.type in ("stop", "stop_limit")
            ),
            None
        )

        exit_leg = next(
            (leg for leg in legs if leg.status == "filled"),
            None
        )

        if exit_leg is None:
            open_trades.append({
                "symbol": order.symbol,
                "side": order.side,
                "qty": entry_qty,
                "entryPrice": entry_price,
                "filledAt": _to_iso(order.filled_at),
                "tp": (
                    float(take_profit.limit_price)
                    if take_profit
                    else None
                ),
                "sl": (
                    float(stop_loss.stop_price)
                    if stop_loss
                    else None
                ),
                "source": "live",
            })
            continue

        exit_price = float(exit_leg.filled_avg_price)
        exit_qty = float(exit_leg.filled_qty)

        if order.side == "buy":
            pnl = (exit_price - entry_price) * exit_qty
        else:
            pnl = (entry_price - exit_price) * exit_qty

        pnl_pct = (pnl / (entry_price * exit_qty)) * 100

        closed_trades.append({
            "symbol": order.symbol,
            "side": order.side,
            "qty": exit_qty,
            "entryPrice": entry_price,
            "exitPrice": exit_price,
            "outcome": "tp" if exit_leg.type == "limit" else "sl",
            "pnl": pnl,
            "pnlPct": pnl_pct,
            "openedAt": _to_iso(order.filled_at),
            "closedAt": _to_iso(exit_leg.filled_at),
            "source": "live",
        })

    return closed_trades, open_trades


def register_dashboard(app):

    api = tradeapi.REST(
        os.environ["ALPACA_API_KEY"],
        os.environ["ALPACA_SECRET_KEY"],
        os.environ.get(
            "ALPACA_BASE_URL",
            "https://paper-api.alpaca.markets"
        )
    )

    @app.route("/dashboard")
    def dashboard_page():
        return render_template("dashboard.html")

    @app.route("/montecarlo")
    def monte_carlo():
        return render_template("monte_carlo_simulation.html")

    @app.route("/montecarlo/returns")
    def montecarlo_returns():
        closed, _opened, files = _load_simulation_trades()
        returns = []
        wins = 0

        for trade in closed:
            ret = float(trade["pnlPct"]) / 100.0
            returns.append(ret)
            if ret > 0:
                wins += 1

        return jsonify({
            "returns": returns,
            "files": files,
            "count": len(returns),
            "winRate": (wins / len(returns)) if returns else 0,
        })

    @app.route("/dashboard/data")
    def dashboard_data():
        live_closed, live_open = _collect_live_trades(api)
        sim_closed, sim_open, sim_files = _load_simulation_trades()

        sim_closed, sim_open = _drop_overlapping_sim(
            sim_closed,
            sim_open,
            live_closed,
            live_open
        )

        closed_trades = live_closed + sim_closed
        open_trades = live_open + sim_open

        closed_trades.sort(key=lambda trade: trade["closedAt"] or "")
        open_trades.sort(key=lambda trade: trade["filledAt"] or "")

        return jsonify({
            "closed": closed_trades,
            "open": open_trades,
            "simFiles": sim_files,
        })
