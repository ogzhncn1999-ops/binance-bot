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
INTERVAL = "15m"
TRAILING_STOP_PERCENT = 0.015  # %1.5 Trailing Stop
MAX_POSITIONS = 7              # En fazla 7 açık pozisyon
ALLOCATION_PER_TRADE = 0.10    # Bakiyenin %10'u

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", 
    "XRPUSDT", "BNBUSDT", "NEARUSDT", "DOGEUSDT", "ADAUSDT"
]

# En yüksek fiyat takibi için bellekte tutulan dict
highest_prices = {}


def send_signed_request(method, endpoint, params=None):
    """Binance Futures API imza ve istek yardımcısı"""
    if params is None:
        params = {}

    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 50000

    # Parametreleri alfabetik sırala
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


def get_klines(symbol, limit=60):
    """Kapanış fiyatlarını alır"""
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
    """EMA Hesaplama"""
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price * k) + (ema[-1] * (1 - k)))
    return ema


def get_active_positions():
    """Açık pozisyonları filtreler"""
    res = send_signed_request("GET", "/fapi/v2/positionRisk")
    active = {}
    if isinstance(res, list):
        for pos in res:
            amt = float(pos.get("positionAmt", 0))
            if amt != 0:
                symbol = pos.get("symbol")
                entry_price = float(pos.get("entryPrice", 0))
                active[symbol] = {
                    "amount": amt,
                    "entry_price": entry_price
                }
    return active


def get_usdt_balance():
    """Kullanılabilir USDT Bakiyesini Alır"""
    res = send_signed_request("GET", "/fapi/v2/account")
    if isinstance(res, dict) and "assets" in res:
        for asset in res["assets"]:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
    return 0.0


def analyze_opportunities(active_symbols):
    """Watchlist içindeki fırsatları analiz eder ve loglar"""
    candidates = []
    print(f"\n--- Market Taramasi Basladi ({len(WATCHLIST) - len(active_symbols)} coin taraniyor) ---")
    
    for symbol in WATCHLIST:
        if symbol in active_symbols:
            continue

        closes = get_klines(symbol)
        if len(closes) < 50:
            print(f"[{symbol}] Yetersiz mum verisi ({len(closes)}/50)")
            continue

        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)

        current_price = closes[-1]
        last_ema20, prev_ema20 = ema20[-1], ema20[-2]
        last_ema50, prev_ema50 = ema50[-1], ema50[-2]

        # EMA20, EMA50'yi yukarı kesiyor mu?
        ema_cross_up = (prev_ema20 <= prev_ema50) and (last_ema20 > last_ema50)

        if ema_cross_up:
            score = ((last_ema20 - last_ema50) / last_ema50) * 100
            print(f"[{symbol}] 🔥 SINYAL YAKALANDI! (Fiyat: {current_price} | EMA20: {round(last_ema20, 4)} > EMA50: {round(last_ema50, 4)})")
            candidates.append({
                "symbol": symbol,
                "price": current_price,
                "score": score
            })
        else:
            print(f"[{symbol}] Taranıyor... Fiyat: {current_price} | EMA20: {round(last_ema20, 4)} | EMA50: {round(last_ema50, 4)} (Kesişim yok)")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def execute_buy(symbol, price):
    """Piyasa emriyle alım gerçekleştirir"""
    balance = get_usdt_balance()
    if balance <= 10:
        print(f"[{symbol}] Yetersiz bakiye: {balance} USDT")
        return

    trade_amount_usdt = balance * ALLOCATION_PER_TRADE
    qty = round(trade_amount_usdt / price, 3)

    if qty <= 0:
        print(f"[{symbol}] Miktar cok dusuk: {qty}")
        return

    params = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    }
    
    res = send_signed_request("POST", "/fapi/v1/order", params)
    print(f"[{symbol}] BUY Emir Sonucu: {res}")
    
    # En yüksek fiyat takibini başlat
    highest_prices[symbol] = price


def manage_trailing_stops(active_positions):
    """Açık pozisyonlar için Trailing Stop kontrolü yapar"""
    for symbol, pos_data in active_positions.items():
        closes = get_klines(symbol, limit=2)
        if not closes:
            continue

        current_price = closes[-1]
        
        # En yüksek fiyatı güncelle
        if symbol not in highest_prices:
            highest_prices[symbol] = max(pos_data["entry_price"], current_price)
        else:
            highest_prices[symbol] = max(highest_prices[symbol], current_price)

        stop_price = highest_prices[symbol] * (1 - TRAILING_STOP_PERCENT)

        print(f"[{symbol}] Pozisyon Izleniyor -> Guncel: {current_price} | Zirve: {highest_prices[symbol]} | Stop: {round(stop_price, 4)}")

        if current_price <= stop_price:
            print(f"[{symbol}] 🛑 TRAILING STOP TETIKLENDI! Satis Yapiliyor...")
            params = {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": abs(pos_data["amount"]),
                "reduceOnly": "true"
            }
            res = send_signed_request("POST", "/fapi/v1/order", params)
            print(f"[{symbol}] SELL Emir Sonucu: {res}")
            if symbol in highest_prices:
                del highest_prices[symbol]


def bot_loop():
    """Bot Ana Döngüsü"""
    print("Coklu Coin Multi-Pair Botu Baslatildi...")
    while True:
        try:
            active_positions = get_active_positions()
            active_symbols = list(active_positions.keys())

            # 1. Trailing Stop Kontrolü
            if active_positions:
                manage_trailing_stops(active_positions)

            # 2. Yeni Alım Fırsatları Taraması
            if len(active_positions) < MAX_POSITIONS:
                candidates = analyze_opportunities(active_symbols)
                
                # En yüksek skorlu 1 adedini al
                if candidates:
                    top_candidate = candidates[0]
                    print(f"[{top_candidate['symbol']}] SINYAL YAKALANDI! Alim Yapiliyor...")
                    execute_buy(top_candidate['symbol'], top_candidate['price'])

        except Exception as e:
            print(f"[Ana Dongu Hatasi]: {e}")

        time.sleep(10)


# Botu arka planda başlat
threading.Thread(target=bot_loop, daemon=True).start()


@app.route('/')
def home():
    active_positions = get_active_positions()
    return jsonify({
        "status": "Multi-Pair Bot Active",
        "active_positions": list(active_positions.keys()),
        "active_positions_count": len(active_positions),
        "max_allowed": MAX_POSITIONS
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
