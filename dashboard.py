from flask import jsonify, render_template
import alpaca_trade_api as tradeapi
import os


def register_dashboard(app):

    # =========================
    # ALPACA CLIENT
    # =========================

    api = tradeapi.REST(
        os.environ["ALPACA_API_KEY"],
        os.environ["ALPACA_SECRET_KEY"],
        os.environ.get(
            "ALPACA_BASE_URL",
            "https://paper-api.alpaca.markets"
        )
    )

    # =========================
    # DASHBOARD PAGE
    # =========================

    @app.route("/dashboard")
    def dashboard_page():
        return render_template("dashboard.html")

    # =========================
    # DASHBOARD DATA
    # =========================

    @app.route("/dashboard/data")
    def dashboard_data():

        orders = api.list_orders(
            status="all",
            limit=500,
            nested=True
        )

        closed_trades = []
        open_trades = []

        for order in orders:

            # We only care about bracket orders
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

            # Find TP and SL legs
            take_profit = next(
                (
                    leg for leg in legs
                    if leg.type == "limit"
                ),
                None
            )

            stop_loss = next(
                (
                    leg for leg in legs
                    if leg.type in ("stop", "stop_limit")
                ),
                None
            )

            # Find the exit that actually filled
            exit_leg = next(
                (
                    leg for leg in legs
                    if leg.status == "filled"
                ),
                None
            )

            # =========================
            # OPEN TRADE
            # =========================

            if exit_leg is None:

                open_trades.append({
                    "symbol": order.symbol,
                    "side": order.side,
                    "qty": entry_qty,
                    "entryPrice": entry_price,
                    "filledAt": order.filled_at,
                    "tp": (
                        float(take_profit.limit_price)
                        if take_profit
                        else None
                    ),
                    "sl": (
                        float(stop_loss.stop_price)
                        if stop_loss
                        else None
                    )
                })

                continue

            # =========================
            # CLOSED TRADE
            # =========================

            exit_price = float(
                exit_leg.filled_avg_price
            )

            exit_qty = float(
                exit_leg.filled_qty
            )

            if order.side == "buy":
                pnl = (
                              exit_price - entry_price
                      ) * exit_qty
            else:
                pnl = (
                              entry_price - exit_price
                      ) * exit_qty

            pnl_pct = (
                              pnl / (entry_price * exit_qty)
                      ) * 100

            outcome = (
                "tp"
                if exit_leg.type == "limit"
                else "sl"
            )

            closed_trades.append({
                "symbol": order.symbol,
                "side": order.side,
                "qty": exit_qty,
                "entryPrice": entry_price,
                "exitPrice": exit_price,
                "outcome": outcome,
                "pnl": pnl,
                "pnlPct": pnl_pct,
                "openedAt": order.filled_at,
                "closedAt": exit_leg.filled_at
            })

        # =========================
        # SORT
        # =========================

        closed_trades.sort(
            key=lambda trade: trade["closedAt"] or "",
            reverse=True
        )

        open_trades.sort(
            key=lambda trade: trade["filledAt"] or "",
            reverse=True
        )

        # =========================
        # RESPONSE
        # =========================

        return jsonify({
            "closed": closed_trades,
            "open": open_trades
        })