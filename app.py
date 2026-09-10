import os
import time
import hmac
import hashlib
import urllib.parse
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# Render Environment Variables kontrolleri
API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()

BASE_URL = "https://testnet.binancefuture.com"
INTERVAL = "15m"               # 15 dakikalık grafikler
TRAILING_STOP_PERCENT = 0.015  # %1.5 Trailing Stop
MAX_POSITIONS = 7              # En fazla 7 açık pozisyon
ALLOCATION_PER_TRADE = 0.10    # Bakiyenin %10'u

FAST_EMA_PERIOD = 9
SLOW_EMA_PERIOD = 21

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", 
    "XRPUSDT", "BNBUSDT", "NEARUSDT", "DOGEUSDT", "ADAUSDT",
    "LINKUSDT", "LTCUSDT", "MATICUSDT", "DOTUSDT", "UNIUSDT",
    "ATOMUSDT", "ARBUSDT", "OPUSDT", "APTUSDT"
]

highest_prices = {}  # Long pozisyonlar için zirve takibi
lowest_prices = {}   # Short pozisyonlar için dip takibi


def send_signed_request(method, endpoint, params=None):
    if params is None:
        params = {}

    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 50000

    sorted_params = sorted(params.items())
    query_string = urllib.parse.urlencode(sorted_params)

    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    full_url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}

    try:
        if method.upper() == "GET":
            response = requests.get(full_url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            response = requests.post(full_url, headers=headers, timeout=10)
        elif method.upper() == "DELETE":
            response = requests.delete(full_url, headers=headers, timeout=10)
        return response.json()
    except Exception as e:
        print(f"[API Hatasi] {endpoint}: {e}")
        return None


def get_klines(symbol, limit=50):
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit={limit}"
    try:
        res = requests.get(url, timeout=10).json()
        if isinstance(res, list):
            closes = [float(k[4]) for k in res]
            return closes
    except Exception as e:
        print(f"[{symbol}] Klines alma hatası: {e}")
    return []


def calculate_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price * k) + (ema[-1] * (1 - k)))
    return ema


def get_active_positions():
    res = send_signed_request("GET", "/fapi/v2/positionRisk")
    active = {}
    if isinstance(res, list):
        for pos in res:
            amt = float(pos.get("positionAmt", 0))
            if amt != 0:
                symbol = pos.get("symbol")
                entry_price = float(pos.get("entryPrice", 0))
                side = "LONG" if amt > 0 else "SHORT"
                active[symbol] = {
                    "amount": amt,
                    "entry_price": entry_price,
                    "side": side
                }
    return active


def get_usdt_balance():
    res = send_signed_request("GET", "/fapi/v2/account")
    if isinstance(res, dict) and "assets" in res:
        for asset in res["assets"]:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
    return 0.0


def analyze_opportunities(active_symbols):
    candidates = []
    print(f"\n--- Çift Yönlü Market Taraması Başladı ({len(WATCHLIST) - len(active_symbols)} coin taranıyor) ---")
    
    for symbol in WATCHLIST:
        if symbol in active_symbols:
            continue

        closes = get_klines(symbol)
        if len(closes) < SLOW_EMA_PERIOD + 2:
            print(f"[{symbol}] Yetersiz mum verisi")
            continue

        ema_fast = calculate_ema(closes, FAST_EMA_PERIOD)
        ema_slow = calculate_ema(closes, SLOW_EMA_PERIOD)

        current_price = closes[-1]
        last_fast, prev_fast = ema_fast[-1], ema_fast[-2]
        last_slow, prev_slow = ema_slow[-1], ema_slow[-2]

        is_long_cross = (prev_fast <= prev_slow) and (last_fast > last_slow)
        is_long_trend = (last_fast > last_slow) and (current_price > last_fast)

        is_short_cross = (prev_fast >= prev_slow) and (last_fast < last_slow)
        is_short_trend = (last_fast < last_slow) and (current_price < last_fast)

        if is_long_cross or is_long_trend:
            score = abs((last_fast - last_slow) / last_slow) * 100
            signal_type = "LONG (CROSS UP)" if is_long_cross else "LONG (TREND KATILIMI)"
            print(f"[{symbol}] 🚀 LONG SİNYALİ! Tip: {signal_type} | Fiyat: {current_price} | EMA9: {round(last_fast, 4)} > EMA21: {round(last_slow, 4)}")
            candidates.append({
                "symbol": symbol,
                "price": current_price,
                "side": "BUY",
                "score": score
            })
        elif is_short_cross or is_short_trend:
            score = abs((last_fast - last_slow) / last_slow) * 100
            signal_type = "SHORT (CROSS DOWN)" if is_short_cross else "SHORT (TREND KATILIMI)"
            print(f"[{symbol}] 🔻 SHORT SİNYALİ! Tip: {signal_type} | Fiyat: {current_price} | EMA9: {round(last_fast, 4)} < EMA21: {round(last_slow, 4)}")
            candidates.append({
                "symbol": symbol,
                "price": current_price,
                "side": "SELL",
                "score": score
            })
        else:
            print(f"[{symbol}] Taranıyor... Fiyat: {current_price} | EMA9: {round(last_fast, 4)} | EMA21: {round(last_slow, 4)} (Uygun sinyal yok)")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def execute_order(symbol, price, side):
    balance = get_usdt_balance()
    if balance <= 10:
        print(f"[{symbol}] Yetersiz bakiye: {balance} USDT")
        return

    trade_amount_usdt = balance * ALLOCATION_PER_TRADE
    qty = round(trade_amount_usdt / price, 3)

    # Minimum lot sınırını kontrol eden düzeltme
    if symbol == "BTCUSDT" and qty < 0.001:
        qty = 0.001

    if qty <= 0:
        print(f"[{symbol}] Miktar çok düşük: {qty}")
        return

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    }
    
    res = send_signed_request("POST", "/fapi/v1/order", params)
    print(f"[{symbol}] {side} Emir Sonucu: {res}")
    
    if side == "BUY":
        highest_prices[symbol] = price
    else:
        lowest_prices[symbol] = price


def manage_trailing_stops(active_positions):
    for symbol, pos_data in active_positions.items():
        closes = get_klines(symbol, limit=2)
        if not closes:
            continue

        current_price = closes[-1]
        side = pos_data["side"]

        if side == "LONG":
            if symbol not in highest_prices:
                highest_prices[symbol] = max(pos_data["entry_price"], current_price)
            else:
                highest_prices[symbol] = max(highest_prices[symbol], current_price)

            stop_price = highest_prices[symbol] * (1 - TRAILING_STOP_PERCENT)
            print(f"[{symbol}] LONG İzleniyor -> Güncel: {current_price} | Zirve: {highest_prices[symbol]} | Stop: {round(stop_price, 4)}")

            if current_price <= stop_price:
                print(f"[{symbol}] 🛑 LONG TRAILING STOP TETİKLENDİ! Pozisyon kapatılıyor...")
                params = {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": abs(pos_data["amount"]),
                    "reduceOnly": "true"
                }
                res = send_signed_request("POST", "/fapi/v1/order", params)
                print(f"[{symbol}] LONG Kapatma Sonucu: {res}")
                if symbol in highest_prices:
                    del highest_prices[symbol]

        elif side == "SHORT":
            if symbol not in lowest_prices:
                lowest_prices[symbol] = min(pos_data["entry_price"], current_price)
            else:
                lowest_prices[symbol] = min(lowest_prices[symbol], current_price)

            stop_price = lowest_prices[symbol] * (1 + TRAILING_STOP_PERCENT)
            print(f"[{symbol}] SHORT İzleniyor -> Güncel: {current_price} | Dip: {lowest_prices[symbol]} | Stop: {round(stop_price, 4)}")

            if current_price >= stop_price:
                print(f"[{symbol}] 🛑 SHORT TRAILING STOP TETİKLENDİ! Pozisyon kapatılıyor...")
                params = {
                    "symbol": symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": abs(pos_data["amount"]),
                    "reduceOnly": "true"
                }
                res = send_signed_request("POST", "/fapi/v1/order", params)
                print(f"[{symbol}] SHORT Kapatma Sonucu: {res}")
                if symbol in lowest_prices:
                    del lowest_prices[symbol]


def bot_loop():
    print("Çift Yönlü Multi-Pair Bot Başlatıldı...")
    while True:
        try:
            active_positions = get_active_positions()
            active_symbols = list(active_positions.keys())

            if active_positions:
                manage_trailing_stops(active_positions)

            if len(active_positions) < MAX_POSITIONS:
                candidates = analyze_opportunities(active_symbols)
                
                if candidates:
                    top_candidate = candidates[0]
                    print(f"[{top_candidate['symbol']}] SİNYAL ONAYLANDI! Yön: {top_candidate['side']} İşlem Yapılıyor...")
                    execute_order(top_candidate['symbol'], top_candidate['price'], top_candidate['side'])

        except Exception as e:
            print(f"[Ana Dongu Hatasi]: {e}")

        time.sleep(10)


threading.Thread(target=bot_loop, daemon=True).start()


@app.route('/')
def home():
    active_positions = get_active_positions()
    return jsonify({
        "status": "Bi-Directional Multi-Pair Bot Active",
        "active_positions": list(active_positions.keys()),
        "active_positions_count": len(active_positions),
        "max_allowed": MAX_POSITIONS
    })


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
