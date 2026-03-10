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

    if not data or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    symbol = data.get("symbol")
    side   = data.get("side")    # "buy" or "sell"
    qty    = data.get("qty")
    tp     = data.get("tp")      # take profit price
    sl     = data.get("sl")      # stop loss price

    if not all([symbol, side, qty, tp, sl]):
        return jsonify({"error": "Missing required fields: symbol, side, qty, tp, sl"}), 400

    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            type="market",
            time_in_force="day",
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
            "sl": sl
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
