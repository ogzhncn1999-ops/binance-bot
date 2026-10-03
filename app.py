import os
import time
import hmac
import hashlib
import math
import requests
from flask import Flask
from threading import Thread
from urllib.parse import urlencode

app = Flask(__name__)

# ============================================================
# BINANCE / CANLI İŞLEM AYARLARI
# ============================================================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
TESTNET = os.environ.get("TESTNET", "False").lower() == "true"

BASE_URL = (
    "https://testnet.binancefuture.com"
    if TESTNET
    else "https://fapi.binance.com"
)

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "BINANCE_API_KEY ve BINANCE_API_SECRET environment değişkenlerini tanımlayın."
    )

# ============================================================
# STRATEJİ
# ============================================================
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
    "NEARUSDT", "APTUSDT", "ATOMUSDT"
]

INTERVAL = "4h"
LEVERAGE = 3
TRADE_USDT = 10.0
MAX_ACTIVE_POSITIONS = 1

MIN_SIGNAL_SCORE = 8
VOLUME_MULTIPLIER = 1.15
MIN_ATR_THRESHOLD = 0.002

STOP_LOSS_PCT = 0.025
TAKE_PROFIT_PCT = 0.075

KLINE_LIMIT = 250
LOOP_SECONDS = 90  # Ortak IP ban riskini azaltmak için döngü süresi esnetildi

BOT_ORDER_PREFIX = "BOT4H"

symbol_step_sizes = {}
symbol_precisions = {}
symbol_tick_sizes = {}
available_symbols = set()
last_processed_candle = None

session = requests.Session()


def log(message):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} {message}", flush=True)


def true_url_format(params):
    return {k: v for k, v in params.items() if v is not None}


def send_signed_request(http_method, url_path, payload=None, retries=2):
    if payload is None:
        payload = {}

    for attempt in range(retries + 1):
        try:
            params = true_url_format(payload.copy())
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", 5000)

            query_string = urlencode(params)
            signature = hmac.new(
                API_SECRET.encode("utf-8"),
                query_string.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()

            url = f"{BASE_URL}{url_path}?{query_string}&signature={signature}"

            response = session.request(
                method=http_method,
                url=url,
                headers={"X-MBX-APIKEY": API_KEY},
                timeout=10
            )

            try:
                data = response.json()
            except Exception:
                data = {"code": response.status_code, "msg": response.text[:500]}

            if response.ok and not (
                isinstance(data, dict) and data.get("code", 0) < 0
            ):
                return data

            log(f"API HATASI {url_path}: HTTP={response.status_code} DATA={data}")

            if response.status_code == 418 or (
                isinstance(data, dict) and data.get("code") == -1003
            ):
                log("BINANCE 418/-1003: IP rate-limit ban. 60 saniye bekleniyor.")
                time.sleep(60)
                return {}

            if response.status_code == 429:
                time.sleep(10 + attempt * 5)
                continue

            return {}

        except Exception as e:
            log(f"API İstek Hatası {url_path}: {e}")
            if attempt < retries:
                time.sleep(3)
            else:
                return {}

    return {}


def load_exchange_info():
    global symbol_step_sizes, symbol_precisions
    global symbol_tick_sizes, available_symbols

    url = f"{BASE_URL}/fapi/v1/exchangeInfo"

    while True:
        try:
            res = session.get(url, timeout=10).json()

            if "symbols" not in res:
                raise RuntimeError(res)

            for s in res["symbols"]:
                sym = s["symbol"]

                if s.get("status") != "TRADING":
                    continue
                if s.get("quoteAsset") != "USDT":
                    continue

                available_symbols.add(sym)
                symbol_precisions[sym] = int(
                    s.get("quantityPrecision", 3)
                )

                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        symbol_step_sizes[sym] = float(f["stepSize"])
                    elif f["filterType"] == "PRICE_FILTER":
                        symbol_tick_sizes[sym] = float(f["tickSize"])

            log("Binance exchangeInfo yüklendi.")
            return True

        except Exception as e:
            log(f"ExchangeInfo hatası: {e}")
            time.sleep(10)


def set_leverage(symbol):
    res = send_signed_request(
        "POST",
        "/fapi/v1/leverage",
        {"symbol": symbol, "leverage": LEVERAGE}
    )
    return bool(res)


def get_market_data(symbol, interval, limit=250):
    url = (
        f"{BASE_URL}/fapi/v1/klines"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )

    try:
        res = session.get(url, timeout=10).json()

        if not isinstance(res, list) or len(res) < 60:
            return None

        candles = []
        for x in res:
            candles.append({
                "open_time": int(x[0]),
                "close_time": int(x[6]),
                "high": float(x[2]),
                "low": float(x[3]),
                "close": float(x[4]),
                "volume": float(x[5])
            })

        return candles[:-1]

    except Exception as e:
        log(f"KLINE Hatası ({symbol}): {e}")
        return None


def calculate_ema(data, period):
    if len(data) < period:
        return None

    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period

    for price in data[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def calculate_ema_series(data, period):
    if len(data) < period:
        return []

    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    result = [ema]

    for price in data[period:]:
        ema = (price - ema) * multiplier + ema
        result.append(ema)

    return result


def calculate_rsi(data, period=14):
    if len(data) < period + 1:
        return 50.0

    deltas = [data[i] - data[i - 1] for i in range(1, len(data))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0

    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        ))

    if len(tr) < period:
        return 0.0

    atr = sum(tr[:period]) / period
    for value in tr[period:]:
        atr = ((atr * (period - 1)) + value) / period

    return atr


def calculate_bollinger_bands(closes, period=20, std_dev=2):
    if len(closes) < period:
        return None, None, None

    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    stdev = math.sqrt(variance)

    return (
        sma + std_dev * stdev,
        sma,
        sma - std_dev * stdev
    )


def calculate_macd_series(closes, fast=12, slow=26, signal_period=9):
    if len(closes) < slow + signal_period:
        return None

    fast_series = calculate_ema_series(closes, fast)
    slow_series = calculate_ema_series(closes, slow)

    offset = slow - fast
    fast_aligned = fast_series[offset:]

    macd = [
        f - s for f, s in zip(fast_aligned, slow_series)
    ]

    if len(macd) < signal_period:
        return None

    signal = calculate_ema_series(macd, signal_period)
    if not signal:
        return None

    macd_aligned = macd[signal_period - 1:]

    return macd_aligned, signal


def round_step(value, step):
    value = float(value)
    step = float(step)

    if step <= 0:
        return value

    return math.floor(value / step + 1e-12) * step


def format_qty(symbol, amount):
    step = float(symbol_step_sizes.get(symbol, 0.001))
    precision = int(symbol_precisions.get(symbol, 3))
    rounded = round_step(float(amount), step)
    return f"{rounded:.{precision}f}"


def format_price(symbol, price):
    tick = float(symbol_tick_sizes.get(symbol, 0.01))
    price = float(price)

    if tick <= 0:
        return str(price)

    rounded = round_step(price, tick)

    if tick < 1:
        decimals = max(0, int(round(-math.log10(tick))))
    else:
        decimals = 0

    return f"{rounded:.{decimals}f}"


def get_all_positions():
    res = send_signed_request("GET", "/fapi/v2/positionRisk")
    positions = []

    if isinstance(res, list):
        for pos in res:
            try:
                if abs(float(pos["positionAmt"])) > 0:
                    positions.append(pos)
            except Exception:
                pass

    return positions


def get_position(symbol):
    for pos in get_all_positions():
        if pos["symbol"] == symbol:
            return pos
    return None


def get_balance():
    res = send_signed_request("GET", "/fapi/v2/account")

    if isinstance(res, dict) and "assets" in res:
        for asset in res["assets"]:
            if asset["asset"] == "USDT":
                return (
                    float(asset["walletBalance"]),
                    float(asset["availableBalance"])
                )

    return 0.0, 0.0


def open_market_order(symbol, side, qty):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": format_qty(symbol, qty),
        "newOrderRespType": "RESULT"
    }

    res = send_signed_request("POST", "/fapi/v1/order", params)
    return res


def close_position_market(symbol, pos_amt):
    side = "SELL" if float(pos_amt) > 0 else "BUY"
    qty = format_qty(symbol, abs(float(pos_amt)))

    res = send_signed_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
            "newOrderRespType": "RESULT"
        }
    )
    return res


def bot_client_id(symbol, kind):
    return f"{BOT_ORDER_PREFIX}{kind}{symbol}{int(time.time()) % 1000000}"[:32]


def place_protection_orders(symbol, position_amt, entry_price):
    position_amt = float(position_amt)
    entry_price = float(entry_price)
    qty = format_qty(symbol, abs(position_amt))

    if position_amt > 0:
        stop_price = entry_price * (1 - float(STOP_LOSS_PCT))
        take_price = entry_price * (1 + float(TAKE_PROFIT_PCT))
        exit_side = "SELL"
    else:
        stop_price = entry_price * (1 + float(STOP_LOSS_PCT))
        take_price = entry_price * (1 - float(TAKE_PROFIT_PCT))
        exit_side = "BUY"

    stop_price = format_price(symbol, stop_price)
    take_price = format_price(symbol, take_price)

    send_signed_request(
        "POST",
        "/fapi/v1/algoOrder",
        {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "quantity": qty,
            "triggerPrice": stop_price,
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "timeInForce": "GTC",
            "priceProtect": "true",
            "clientAlgoId": bot_client_id(symbol, "SL")
        }
    )

    time.sleep(0.5)

    send_signed_request(
        "POST",
        "/fapi/v1/algoOrder",
        {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "quantity": qty,
            "triggerPrice": take_price,
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "timeInForce": "GTC",
            "priceProtect": "true",
            "clientAlgoId": bot_client_id(symbol, "TP")
        }
    )

    return True


def get_open_algo_orders(symbol):
    res = send_signed_request(
        "GET",
        "/fapi/v1/openAlgoOrders",
        {"symbol": symbol}
    )
    return res if isinstance(res, list) else []


def build_signal(symbol, candles):
    closes = [x["close"] for x in candles]
    highs = [x["high"] for x in candles]
    lows = [x["low"] for x in candles]
    volumes = [x["volume"] for x in candles]

    if len(closes) < 210:
        return None

    price = closes[-1]

    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)
    ema21_prev = calculate_ema(closes[:-1], 21)

    rsi = calculate_rsi(closes, 14)
    atr = calculate_atr(highs, lows, closes, 14)

    upper_b, mid_b, lower_b = calculate_bollinger_bands(closes, 20, 2)
    macd_data = calculate_macd_series(closes)

    if any(x is None for x in (ema9, ema21, ema50, ema200, ema21_prev, upper_b, mid_b, lower_b)):
        return None

    if not macd_data:
        return None

    macd_values, signal_values = macd_data
    if len(macd_values) < 2 or len(signal_values) < 2:
        return None

    macd_now = macd_values[-1]
    macd_prev = macd_values[-2]
    signal_now = signal_values[-1]
    signal_prev = signal_values[-2]

    hist_now = macd_now - signal_now
    hist_prev = macd_prev - signal_prev

    avg_volume = sum(volumes[-11:-1]) / 10
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 0

    if volume_ratio < VOLUME_MULTIPLIER:
        return None

    atr_ratio = atr / price if price > 0 else 0

    bullish_cross = macd_prev <= signal_prev and macd_now > signal_now
    bearish_cross = macd_prev >= signal_prev and macd_now < signal_now
    bullish_momentum = macd_now > signal_now and hist_now > hist_prev
    bearish_momentum = macd_now < signal_now and hist_now < hist_prev

    ema21_rising = ema21 > ema21_prev
    ema21_falling = ema21 < ema21_prev

    bb_long_ok = price > mid_b and price < upper_b
    bb_short_ok = price < mid_b and price > lower_b

    # LONG SCORE
    long_score = 0
    if price > ema200: long_score += 2
    if ema9 > ema21: long_score += 1
    if ema21 > ema50: long_score += 1
    if ema21_rising: long_score += 1
    if bullish_cross: long_score += 2
    elif bullish_momentum: long_score += 1
    if 52 <= rsi <= 64: long_score += 1
    if bb_long_ok: long_score += 1
    if atr_ratio >= MIN_ATR_THRESHOLD: long_score += 1

    # SHORT SCORE
    short_score = 0
    if price < ema200: short_score += 2
    if ema9 < ema21: short_score += 1
    if ema21 < ema50: short_score += 1
    if ema21_falling: short_score += 1
    if bearish_cross: short_score += 2
    elif bearish_momentum: short_score += 1
    if 36 <= rsi <= 48: short_score += 1
    if bb_short_ok: short_score += 1
    if atr_ratio >= MIN_ATR_THRESHOLD: short_score += 1

    best_score = max(long_score, short_score)
    if best_score < MIN_SIGNAL_SCORE:
        return None

    if long_score > short_score:
        side = "BUY"
        score = long_score
    elif short_score > long_score:
        side = "SELL"
        score = short_score
    else:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "score": score,
        "price": price,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "candle_close": candles[-1]["close_time"]
    }


def open_trade(signal):
    symbol = signal["symbol"]
    side = signal["side"]

    _, available = get_balance()
    if available < TRADE_USDT:
        log(f"İşlem açılmadı: Bakiye yetersiz ({available:.2f} USDT)")
        return False

    if not set_leverage(symbol):
        return False

    current_price = float(signal["price"])
    notional = float(TRADE_USDT) * float(LEVERAGE)
    raw_qty = notional / current_price
    qty = format_qty(symbol, raw_qty)

    order = open_market_order(symbol, side, qty)
    if not order:
        return False

    time.sleep(1.0)
    position = get_position(symbol)
    if not position:
        return False

    amount = float(position["positionAmt"])
    entry_price = float(position["entryPrice"])

    place_protection_orders(symbol, amount, entry_price)
    log(f"POZİSYON AÇILDI VE KORUNDU: {symbol} {side}")
    return True


def trading_bot_loop():
    global last_processed_candle

    log("OPTIMIZE EDİLMİŞ 4H TREND BOTU BAŞLADI (IP Ban Korumalı).")
    load_exchange_info()

    while True:
        try:
            # IP limitlerini korumak için pozisyon sorgusu her döngüde değil, güvenli aralıkta yapılır
            active_positions = get_all_positions()
            active_symbols = {p["symbol"] for p in active_positions}

            if len(active_positions) >= MAX_ACTIVE_POSITIONS:
                time.sleep(LOOP_SECONDS)
                continue

            potential_signals = []

            for symbol in SYMBOLS:
                if symbol not in available_symbols:
                    continue
                if symbol in active_symbols:
                    continue

                candles = get_market_data(symbol, INTERVAL, KLINE_LIMIT)
                
                # Her istek arasına genişletilmiş rate-limit güvenli bekleme payı
                time.sleep(0.8)

                if not candles:
                    continue

                signal = build_signal(symbol, candles)
                if signal:
                    potential_signals.append(signal)

            if potential_signals:
                potential_signals.sort(key=lambda x: x["score"], reverse=True)
                strongest = potential_signals[0]

                if strongest["candle_close"] != last_processed_candle:
                    last_processed_candle = strongest["candle_close"]
                    log(f"EN GÜÇLÜ SİNYAL: {strongest['symbol']} {strongest['side']} score={strongest['score']}")
                    open_trade(strongest)

            time.sleep(LOOP_SECONDS)

        except Exception as e:
            log(f"BOT DÖNGÜ HATASI: {e}")
            time.sleep(30)


@app.route("/")
def index():
    return "IP-Protected 4H Trend Bot aktif."


if __name__ == "__main__":
    t = Thread(target=trading_bot_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
