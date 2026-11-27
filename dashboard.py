from collections import defaultdict
from datetime import datetime
from pathlib import Path
import csv
import os

from flask import jsonify, render_template


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


TRADES_PAGE_SIZE = 10
_DRAWDOWN_EPSILON = 1e-9


def _num(value):
    parsed = _parse_number(value)
    if parsed is None:
        return 0.0
    return parsed


def _source(trade):
    return trade.get("source") or "live"


def _closed_sort_key(trade):
    parsed = _as_datetime(trade.get("closedAt"))
    if parsed is None:
        return (0, datetime.min)
    return (1, parsed)


def _open_sort_key(trade):
    parsed = _as_datetime(trade.get("filledAt"))
    if parsed is None:
        return (0, datetime.min)
    return (1, parsed)


def sort_closed_chronological(trades):
    """Oldest closed trade first, so equity and period charts run left to right."""
    return sorted(trades, key=_closed_sort_key)


def sort_closed_newest_first(trades):
    """Newest closed trade first. Trades with no close time sort last."""
    return sorted(trades, key=_closed_sort_key, reverse=True)


def sort_open_newest_first(trades):
    return sorted(trades, key=_open_sort_key, reverse=True)


def next_shown(shown, total, action, page_size=TRADES_PAGE_SIZE):
    """Trade-list window. `filter` / `init` snap back to the first page."""
    total = max(0, int(total))
    if action in ("init", "reset", "filter"):
        return min(total, page_size)
    if action == "all":
        return total
    if action == "more":
        current = max(0, int(shown))
        return min(total, current + page_size)
    raise ValueError(f"Unknown trade window action: {action}")


def paginate_trades(trades_newest_first, shown, page_size=TRADES_PAGE_SIZE):
    total = len(trades_newest_first)
    visible = max(0, min(total, int(shown)))
    remaining = total - visible
    return {
        "trades": list(trades_newest_first[:visible]),
        "shown": visible,
        "total": total,
        "remaining": remaining,
        "show_more": remaining > 0,
        "next_count": min(page_size, remaining),
    }


def present_trades(trades, shown, page_size=TRADES_PAGE_SIZE):
    """Newest-first rows, numbered in chronological order (oldest = 1)."""
    newest = sort_closed_newest_first(trades)
    chronological = sort_closed_chronological(trades)
    numbers = {
        id(trade): index + 1
        for index, trade in enumerate(chronological)
    }
    page = paginate_trades(newest, shown, page_size)
    page["rows"] = [
        {
            "closedAt": trade.get("closedAt"),
            "number": numbers[id(trade)],
            "pnl": trade.get("pnl"),
            "source": _source(trade),
            "symbol": trade.get("symbol"),
        }
        for trade in page["trades"]
    ]
    return page


def equity_series(trades, use_pct=False):
    """Cumulative P&L in chronological order. `use_pct` matches the mixed chart."""
    ordered = sort_closed_chronological(trades)
    key = "pnlPct" if use_pct else "pnl"
    running = 0.0
    points = []
    for trade in ordered:
        running += _num(trade.get(key))
        points.append(running)
    return {
        "trades": ordered,
        "points": points,
    }


def is_losing_trade(trade):
    outcome = str(trade.get("outcome") or "").strip().lower()
    return _num(trade.get("pnl")) < 0 or outcome == "sl"


def max_losing_streak(trades):
    best = 0
    streak = 0
    for trade in sort_closed_chronological(trades):
        if is_losing_trade(trade):
            streak += 1
            if streak > best:
                best = streak
        else:
            streak = 0
    return best


def _drawdown_pct(drawdown, peak):
    if abs(drawdown) < _DRAWDOWN_EPSILON:
        return 0.0
    if peak > 0:
        return (drawdown / peak) * 100.0
    return None


def _clean_drawdown(value):
    if abs(value) < _DRAWDOWN_EPSILON:
        return 0.0
    return value


def drawdown_stats(trades):
    """Max dollar drawdown from the running P&L peak, plus that episode's % of peak.

    Current drawdown is the decline from the latest peak to the last trade.
    Percentage is omitted when the peak never rose above zero (an all-loss book
    measured from a flat start).
    """
    ordered = sort_closed_chronological(trades)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_pct = None

    for trade in ordered:
        equity += _num(trade.get("pnl"))
        if equity > peak:
            peak = equity
        drawdown = _clean_drawdown(peak - equity)
        pct = _drawdown_pct(drawdown, peak)
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = pct
        elif (
            abs(drawdown - max_dd) <= _DRAWDOWN_EPSILON
            and pct is not None
            and (max_dd_pct is None or pct > max_dd_pct)
        ):
            max_dd_pct = pct

    current = _clean_drawdown(peak - equity)
    if not ordered:
        return {
            "max_drawdown": 0.0,
            "max_drawdown_pct": None,
            "current_drawdown": 0.0,
            "current_drawdown_pct": None,
            "peak_equity": 0.0,
            "equity": 0.0,
        }

    return {
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "current_drawdown": current,
        "current_drawdown_pct": _drawdown_pct(current, peak),
        "peak_equity": peak,
        "equity": equity,
    }


def payoff_and_expectancy(trades):
    ordered = list(trades)
    if not ordered:
        return {
            "payoff_ratio": None,
            "payoff_infinite": False,
            "expectancy": None,
            "expectancy_pct": None,
            "avg_win": None,
            "avg_loss": None,
        }

    wins = []
    losses = []
    pnl_sum = 0.0
    pct_sum = 0.0
    for trade in ordered:
        pnl = _num(trade.get("pnl"))
        pnl_sum += pnl
        pct_sum += _num(trade.get("pnlPct"))
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)

    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = abs(sum(losses) / len(losses)) if losses else None

    payoff_ratio = None
    payoff_infinite = False
    if avg_win is not None and avg_loss:
        payoff_ratio = avg_win / avg_loss
    elif avg_win is not None and avg_loss is None:
        payoff_infinite = True
    elif avg_win is None and avg_loss is not None:
        payoff_ratio = 0.0

    count = len(ordered)
    return {
        "payoff_ratio": payoff_ratio,
        "payoff_infinite": payoff_infinite,
        "expectancy": pnl_sum / count,
        "expectancy_pct": pct_sum / count,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def risk_report(trades):
    ordered = list(trades)
    report = {
        "count": len(ordered),
        "max_losing_streak": max_losing_streak(ordered),
    }
    report.update(drawdown_stats(ordered))
    report.update(payoff_and_expectancy(ordered))
    if not ordered:
        report["max_drawdown_pct"] = None
        report["current_drawdown_pct"] = None
    return report


def risk_by_source(trades):
    """Separate sim and live books so a blended curve never hides one of them."""
    grouped = {"sim": [], "live": []}
    for trade in trades:
        source = _source(trade)
        grouped.setdefault(source, []).append(trade)
    return {
        source: risk_report(items)
        for source, items in grouped.items()
    }


def source_health(trades):
    def pack(source):
        subset = [trade for trade in trades if _source(trade) == source]
        last = None
        last_dt = None
        pnl = 0.0
        for trade in subset:
            pnl += _num(trade.get("pnl"))
            parsed = _as_datetime(trade.get("closedAt"))
            if parsed is not None and (last_dt is None or parsed > last_dt):
                last_dt = parsed
                last = trade.get("closedAt")
        return {
            "count": len(subset),
            "pnl": pnl,
            "last_closed_at": last,
        }

    last = None
    last_dt = None
    for trade in trades:
        parsed = _as_datetime(trade.get("closedAt"))
        if parsed is not None and (last_dt is None or parsed > last_dt):
            last_dt = parsed
            last = trade.get("closedAt")

    return {
        "sim": pack("sim"),
        "live": pack("live"),
        "last_closed_at": last,
    }


def open_position_risk(position):
    """Notional and adverse dollars to the stop, when a stop is present.

    Buys: (entry - sl) * qty. Sells/shorts: (sl - entry) * qty.
    A negative result means the stop is already through entry (locked-in gain).
    """
    entry = _parse_number(position.get("entryPrice")) or 0.0
    qty = _parse_number(position.get("qty")) or 0.0
    notional = entry * qty
    stop = _parse_number(position.get("sl"))
    if stop is None:
        return {
            "notional": notional,
            "risk": None,
            "risk_pct": None,
        }

    side = str(position.get("side") or "buy").strip().lower()
    if side in ("sell", "short"):
        risk = (stop - entry) * qty
    else:
        risk = (entry - stop) * qty

    risk_pct = (risk / notional * 100.0) if notional else None
    return {
        "notional": notional,
        "risk": risk,
        "risk_pct": risk_pct,
    }


def summarize_open_positions(positions):
    notional = 0.0
    risk = 0.0
    with_sl = 0
    count = 0
    for position in positions:
        count += 1
        row = open_position_risk(position)
        notional += row["notional"]
        if row["risk"] is not None:
            risk += row["risk"]
            with_sl += 1
    return {
        "notional": notional,
        "risk": risk if with_sl else None,
        "positions": count,
        "with_sl": with_sl,
    }


def annotate_open_trade(trade):
    annotated = dict(trade)
    risk = open_position_risk(trade)
    annotated["notional"] = risk["notional"]
    annotated["riskToSl"] = risk["risk"]
    annotated["riskToSlPct"] = risk["risk_pct"]
    return annotated


def _public_book(book):
    return {
        "count": book["count"],
        "pnl": book["pnl"],
        "lastClosedAt": book["last_closed_at"],
    }


def _public_risk(report):
    return {
        "count": report["count"],
        "maxDrawdown": report["max_drawdown"],
        "maxDrawdownPct": report["max_drawdown_pct"],
        "currentDrawdown": report["current_drawdown"],
        "currentDrawdownPct": report["current_drawdown_pct"],
        "peakEquity": report["peak_equity"],
        "equity": report["equity"],
        "maxLosingStreak": report["max_losing_streak"],
        "payoffRatio": report["payoff_ratio"],
        "payoffInfinite": report["payoff_infinite"],
        "expectancy": report["expectancy"],
        "expectancyPct": report["expectancy_pct"],
        "avgWin": report["avg_win"],
        "avgLoss": report["avg_loss"],
    }


def analytics_payload(closed_trades, open_trades):
    health = source_health(closed_trades)
    open_risk = summarize_open_positions(open_trades)
    return {
        "bySource": {
            source: _public_risk(report)
            for source, report in risk_by_source(closed_trades).items()
        },
        "health": {
            "sim": _public_book(health["sim"]),
            "live": _public_book(health["live"]),
            "lastClosedAt": health["last_closed_at"],
        },
        "openRisk": {
            "notional": open_risk["notional"],
            "risk": open_risk["risk"],
            "positions": open_risk["positions"],
            "withSl": open_risk["with_sl"],
        },
    }


def assemble_dashboard_payload(
    live_closed,
    live_open,
    sim_closed,
    sim_open,
    sim_files,
):
    sim_closed, sim_open = _drop_overlapping_sim(
        sim_closed,
        sim_open,
        live_closed,
        live_open,
    )
    closed = sort_closed_newest_first(list(live_closed) + list(sim_closed))
    opened = [
        annotate_open_trade(trade)
        for trade in sort_open_newest_first(list(live_open) + list(sim_open))
    ]
    return {
        "closed": closed,
        "open": opened,
        "simFiles": sim_files,
        "pageSize": TRADES_PAGE_SIZE,
        "analytics": analytics_payload(closed, opened),
    }


def register_dashboard(app):
    import alpaca_trade_api as tradeapi

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

        return jsonify(assemble_dashboard_payload(
            live_closed,
            live_open,
            sim_closed,
            sim_open,
            sim_files,
        ))
