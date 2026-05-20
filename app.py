from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os
import time
from concurrent.futures import ThreadPoolExecutor
from werkzeug.exceptions import HTTPException
from cachetools import TTLCache

app = Flask(__name__)

# --- ENV ---
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise Exception("Missing Alpaca credentials")

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)

# --- THREAD POOL ---
executor = ThreadPoolExecutor(max_workers=5)

# --- DEDUP CACHE (auto cleanup) ---
recent_signals = TTLCache(maxsize=1000, ttl=30)


def is_duplicate(key):
    if key in recent_signals:
        return True
    recent_signals[key] = True
    return False


# --- GLOBAL ERROR HANDLER ---
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return jsonify({"status": "error", "message": e.description}), e.code

    print("🔥 Unhandled error:", str(e), flush=True)
    return jsonify({"status": "error", "message": "Internal server error"}), 500


# --- RETRY ---
def retry(func, attempts=3, delay=1):
    for i in range(attempts):
        try:
            return func()
        except Exception as e:
            print(f"⚠️ Attempt {i+1} failed: {e}", flush=True)
            if i == attempts - 1:
                raise
            time.sleep(delay)


# --- BACKGROUND WORKER ---
def process_order(data):
    try:
        print("📥 Processing:", data, flush=True)

        symbol = data["symbol"]
        side   = data["side"]
        tp     = float(data["tp"])
        sl     = float(data["sl"])

        # --- Dedup ---
        dedup_key = f"{symbol}-{side}-{tp}-{sl}"
        if is_duplicate(dedup_key):
            print("⚠️ Duplicate signal ignored", flush=True)
            return

        # --- Market data ---
        last_trade = retry(lambda: api.get_latest_trade(symbol))
        price = float(last_trade.price)

        if price <= 0:
            print("❌ Invalid price", flush=True)
            return

        # --- Account ---
        account = retry(lambda: api.get_account())
        buying_power = float(account.buying_power)

        notional = round(buying_power * 0.97, 2)
        qty = int(notional / price)

        if qty <= 0:
            print("❌ Not enough buying power", flush=True)
            return

        # --- Order ---
        order = retry(lambda: api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            type="market",
            time_in_force="gtc",
            order_class="bracket",
            take_profit={"limit_price": tp},
            stop_loss={"stop_price": sl}
        ))

        print("✅ Order submitted:", order.id, flush=True)

    except Exception as e:
        print("🔥 ERROR in process_order:", str(e), flush=True)


# --- WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    print("📨 Received:", data, flush=True)

    # --- Auth ---
    if not data or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    # --- Ignored ---
    if data.get("ignored"):
        return jsonify({"status": "ok", "message": "ignored"}), 200

    # --- Validate ---
    required = ["symbol", "side", "tp", "sl"]
    if not all(k in data for k in required):
        return jsonify({
            "status": "error",
            "message": "Missing required fields"
        }), 400

    if data["side"] not in ("buy", "sell"):
        return jsonify({
            "status": "error",
            "message": "Invalid side"
        }), 400

    # --- Async ---
    executor.submit(process_order, data)

    return jsonify({
        "status": "ok",
        "message": "order accepted"
    }), 202


# --- HEALTH ---
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200