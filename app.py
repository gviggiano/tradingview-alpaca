from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os
import time
import uuid
import json
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
CASH_USAGE_PCT = float(os.environ.get("CASH_USAGE_PCT", "85"))
CASH_USAGE_RATIO = CASH_USAGE_PCT / 100


if not 0 < CASH_USAGE_PCT <= 100:
    raise Exception("CASH_USAGE_PCT must be between 0 and 100")

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise Exception("Missing Alpaca credentials")

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)

# --- THREAD POOL ---
executor = ThreadPoolExecutor(max_workers=5)

# --- DEDUP ---
recent_signals = TTLCache(maxsize=1000, ttl=30)


# --- LOG ---
def log(level, rid, msg, **kwargs):
    entry = {
        "level": level,
        "request_id": rid,
        "msg": msg,
        "ts": round(time.time(), 3),
        **kwargs
    }
    print(json.dumps(entry), flush=True)


# --- ERROR HANDLER ---
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return jsonify({"status": "error", "message": e.description}), e.code

    traceback.print_exc()
    return jsonify({"status": "error", "message": "Internal server error"}), 500


# --- RETRY ---
def retry(func, rid, label, attempts=3, delay=1):
    for i in range(attempts):
        try:
            return func()
        except Exception as e:
            log("WARN", rid, f"{label} failed", attempt=i+1, error=str(e))
            if i == attempts - 1:
                raise
            time.sleep(delay)


# --- WORKER ---
def process_order(data, rid):
    try:
        log("INFO", rid, "THREAD START")

        symbol = data["symbol"]
        side   = data["side"]
        tp     = float(data["tp"])
        sl     = float(data["sl"])

        # --- Dedup ---
        key = f"{symbol}-{side}-{tp}-{sl}"
        if key in recent_signals:
            log("WARN", rid, "Duplicate ignored")
            return
        recent_signals[key] = True

        # --- Market price ---
        last_trade = retry(lambda: api.get_latest_trade(symbol), rid, "get_latest_trade")
        price = float(last_trade.price)

        # --- Account ---
        account = retry(lambda: api.get_account(), rid, "get_account")

        cash = float(account.cash)
        buying_power = float(account.buying_power)
        regt_bp = float(getattr(account, "regt_buying_power", 0))

        log("INFO", rid, "ACCOUNT_STATE",
            cash=cash,
            buying_power=buying_power,
            regt_buying_power=regt_bp,
            price=price
        )

        # 🔥 KEY LOGIC: use ONLY settled cash with strong buffer
        usable_funds = cash * CASH_USAGE_RATIO

        qty = int(usable_funds / price)

        log("INFO", rid, "POSITION_SIZING",
            usable_funds=usable_funds,
            qty=qty
        )

        if qty <= 0:
            log("ERROR", rid, "INSUFFICIENT_FUNDS",
                price=price,
                cash=cash,
                usable_funds=usable_funds
            )
            return

        # --- Submit order ---
        try:
            order = retry(lambda: api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force="gtc",
                order_class="bracket",
                take_profit={"limit_price": tp},
                stop_loss={"stop_price": sl}
            ), rid, "submit_order")

            log("INFO", rid, "ORDER_SUCCESS",
                order_id=order.id,
                qty=qty
            )

        except Exception as e:
            log("ERROR", rid, "ORDER_FAILED",
                error=str(e),
                qty=qty,
                price=price
            )
            traceback.print_exc()

    except Exception as e:
        log("ERROR", rid, "PROCESS_FAILED", error=str(e))
        traceback.print_exc()

    finally:
        log("INFO", rid, "THREAD END")


# --- WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    rid = str(uuid.uuid4())
    data = request.get_json()

    log("INFO", rid, "REQUEST_RECEIVED", data=data)

    # --- Auth ---
    if not data or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    if data.get("ignored"):
        return jsonify({"status": "ok", "message": "Request ignored"}), 200

    # --- Validate ---
    required = ["symbol", "side", "tp", "sl"]
    if not all(k in data for k in required):
        return jsonify({"status": "error", "message": "Missing fields"}), 400

    if data["side"] not in ("buy", "sell"):
        return jsonify({"status": "error", "message": "Invalid side"}), 400

    # --- Pre-check using settled cash ---
    try:
        symbol = data["symbol"]

        last_trade = api.get_latest_trade(symbol)
        price = float(last_trade.price)

        account = api.get_account()
        cash = float(account.cash)

        usable_funds = cash * CASH_USAGE_RATIO

        log("INFO", rid, "PRECHECK",
            price=price,
            cash=cash,
            usable_funds=usable_funds
        )

        if usable_funds < price:
            return jsonify({
                "status": "error",
                "message": f"Insufficient settled funds (price={price}, usable_cash={usable_funds})"
            }), 400

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Precheck failed: {str(e)}"
        }), 500

    # --- Async ---
    executor.submit(process_order, data, rid)

    return jsonify({
        "status": "ok",
        "request_id": rid
    }), 202


# --- HEALTH ---
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200