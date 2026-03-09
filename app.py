from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os

app = Flask(__name__)

# Load from environment variables (set these in Railway)
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET")  # your own password
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")  # paper by default

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    # Basic auth check — TradingView alert must include {"secret": "YOUR_SECRET"}
    if not data or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    symbol   = data.get("symbol")    # e.g. "AAPL"
    side     = data.get("side")      # "buy" or "sell"
    qty      = data.get("qty")       # e.g. 10
    order_type = data.get("type", "market")  # "market" or "limit"
    limit_price = data.get("limit_price")    # only needed for limit orders

    if not all([symbol, side, qty]):
        return jsonify({"error": "Missing required fields: symbol, side, qty"}), 400

    try:
        if order_type == "limit" and limit_price:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type="limit",
                limit_price=limit_price,
                time_in_force="gtc"
            )
        else:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force="day"
            )

        return jsonify({
            "status": "order submitted",
            "id": order.id,
            "symbol": order.symbol,
            "qty": order.qty,
            "side": order.side,
            "type": order.type
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
