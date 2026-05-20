from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os

app = Flask(__name__)

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    # a comment to trigger redeploy

    # --- Auth ---
    if not data or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    # --- Ignored signal ---
    if data.get("ignored"):
        return jsonify({"status": "ignored"}), 200

    # --- Parse & validate fields ---
    symbol = data.get("symbol")
    side   = data.get("side")
    tp     = data.get("tp")
    sl     = data.get("sl")

    if not all([symbol, side, tp, sl]):
        return jsonify({"error": "Missing required fields: symbol, side, tp, sl"}), 400

    if side not in ("buy", "sell"):
        return jsonify({"error": "side must be 'buy' or 'sell'"}), 400

    try:
        tp = float(tp)
        sl = float(sl)
    except (TypeError, ValueError):
        return jsonify({"error": "tp and sl must be valid numbers"}), 400

    try:
        # --- Get current price ---
        last_trade = api.get_latest_trade(symbol)
        price = float(last_trade.price)

        # --- Calculate qty from buying power ---
        account      = api.get_account()
        buying_power = float(account.buying_power)
        notional     = round(buying_power * 0.97, 2)

        if notional <= 0:
            return jsonify({"error": "No buying power available"}), 400

        # Bracket orders require qty, not notional
        qty = int(notional / price)

        if qty == 0:
            return jsonify({"error": "Insufficient buying power to buy a single share"}), 400

        # --- Submit bracket order ---
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            type="market",
            time_in_force="gtc",
            order_class="bracket",
            take_profit={"limit_price": tp},
            stop_loss={"stop_price": sl}
        )

        return jsonify({
            "status": "bracket order submitted",
            "id": order.id,
            "symbol": order.symbol,
            "qty": order.qty,
            "side": order.side,
            "tp": tp,
            "sl": sl,
            "price": price
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)