from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os
import time
import uuid
import traceback
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

# --- DEDUP ---
recent_signals = TTLCache(maxsize=1000, ttl=30)


# --- LOGGING HELPER ---
def log(level, request_id, message, **kwargs):
    log_entry = {
        "level": level,
        "request_id": request_id,
        "message": message,
        "ts": round(time.time(), 3),
        **kwargs
    }
    print(log_entry, flush=True)


# --- GLOBAL ERROR HANDLER ---
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return jsonify({"status": "error", "message": e.description}), e.code

    print("🔥 GLOBAL ERROR:", str(e), flush=True)
    traceback.print_exc()
    return jsonify({"status": "error", "message": "Internal server error"}), 500


# --- RETRY ---
def retry(func, request_id, label, attempts=3, delay=1):
    for i in range(attempts):
        try:
            start = time.time()
            result = func()
            elapsed = round(time.time() - start, 3)

            log("INFO", request_id, f"{label} success", attempt=i+1, duration=elapsed)
            return result

        except Exception as e:
            log("WARN", request_id, f"{label} failed", attempt=i+1, error=str(e))

            if i == attempts - 1:
                log("ERROR", request_id, f"{label} final failure", error=str(e))
                raise

            time.sleep(delay)


# --- WORKER ---
def process_order(data, request_id):
    try:
        log("INFO", request_id, "THREAD STARTED", data=data)

        symbol = data["symbol"]
        side   = data["side"]
        tp     = float(data["tp"])
        sl     = float(data["sl"])

        # --- Dedup ---
        dedup_key = f"{symbol}-{side}-{tp}-{sl}"
        if dedup_key in recent_signals:
            log("WARN", request_id, "Duplicate signal ignored", key=dedup_key)
            return

        recent_signals[dedup_key] = True

        # --- Market data ---
        last_trade = retry(lambda: api.get_latest_trade(symbol), request_id, "get_latest_trade")
        price = float(last_trade.price)

        log("INFO", request_id, "Price fetched", price=price)

        if price <= 0:
            log("ERROR", request_id, "Invalid price", price=price)
            return

        # --- Account ---
        account = retry(lambda: api.get_account(), request_id, "get_account")
        buying_power = float(account.buying_power)

        log("INFO", request_id, "Account fetched", buying_power=buying_power)

        notional = round(buying_power * 0.97, 2)
        qty = int(notional / price)

        log("INFO", request_id, "Qty computed",
            price=price,
            buying_power=buying_power,
            notional=notional,
            qty=qty
        )

        if qty <= 0:
            log("ERROR", request_id, "Insufficient buying power",
                price=price,
                buying_power=buying_power,
                notional=notional
            )
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
        ), request_id, "submit_order")

        log("INFO", request_id, "ORDER SUCCESS",
            order_id=order.id,
            symbol=symbol,
            qty=qty,
            side=side
        )

    except Exception as e:
        log("ERROR", request_id, "PROCESS ORDER FAILED", error=str(e))
        traceback.print_exc()

    finally:
        log("INFO", request_id, "THREAD FINISHED")


# --- WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    request_id = str(uuid.uuid4())
    start_time = time.time()

    data = request.get_json()

    log("INFO", request_id, "REQUEST RECEIVED", data=data)

    # --- Auth ---
    if not data or data.get("secret") != WEBHOOK_SECRET:
        log("WARN", request_id, "Unauthorized request")
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    # --- Validate ---
    required = ["symbol", "side", "tp", "sl"]
    if not all(k in data for k in required):
        log("WARN", request_id, "Missing fields", data=data)
        return jsonify({"status": "error", "message": "Missing required fields"}), 400

    if data["side"] not in ("buy", "sell"):
        log("WARN", request_id, "Invalid side", side=data["side"])
        return jsonify({"status": "error", "message": "Invalid side"}), 400

    # --- Pre-check (debug your issue here) ---
    try:
        symbol = data["symbol"]

        last_trade = api.get_latest_trade(symbol)
        price = float(last_trade.price)

        account = api.get_account()
        buying_power = float(account.buying_power)

        log("INFO", request_id, "PRECHECK",
            price=price,
            buying_power=buying_power
        )

        if buying_power < price:
            log("ERROR", request_id, "Precheck failed: insufficient buying power",
                price=price,
                buying_power=buying_power
            )
            return jsonify({
                "status": "error",
                "message": f"Insufficient buying power (price={price}, bp={buying_power})"
            }), 400

    except Exception as e:
        log("ERROR", request_id, "Precheck failed", error=str(e))
        return jsonify({"status": "error", "message": "Precheck failed"}), 500

    # --- Async ---
    executor.submit(process_order, data, request_id)

    elapsed = round(time.time() - start_time, 3)

    log("INFO", request_id, "REQUEST ACCEPTED", duration=elapsed)

    return jsonify({
        "status": "ok",
        "message": "order accepted",
        "request_id": request_id
    }), 202


# --- HEALTH ---
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200