import os
import time
import hmac
import hashlib
import urllib.parse
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# Render Environment Variables
API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()

BASE_URL = "https://testnet.binancefuture.com"
INTERVAL = "15m"               # 15 dakikalık grafikler
TRAILING_STOP_PERCENT = 0.030  # %3.0 Trailing Stop (Genişletilmiş Esnek Stop)
MAX_POSITIONS = 7              # En fazla 7 açık pozisyon
ALLOCATION_PER_TRADE = 0.10    # Bakiyenin %10'u
TARGET_LEVERAGE = 3            # Düşük risk için 3x Kaldıraç

FAST_EMA_PERIOD = 9
SLOW_EMA_PERIOD = 21
RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20

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


def set_leverage(symbol, leverage=TARGET_LEVERAGE):
    """Sembolün kaldıracını otomatik ayarlar"""
    params = {"symbol": symbol, "leverage": leverage}
    res = send_signed_request("POST", "/fapi/v1/leverage", params)
    print(f"[{symbol}] Kaldıraç {leverage}x olarak ayarlandı: {res}")


def get_klines(symbol, limit=60):
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit={limit}"
    try:
        res = requests.get(url, timeout=10).json()
        if isinstance(res, list):
            closes = [float(k[4]) for k in res]
            volumes = [float(k[5]) for k in res]
            return closes, volumes
    except Exception as e:
        print(f"[{symbol}] Klines alma hatası: {e}")
    return [], []


def calculate_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price * k) + (ema[-1] * (1 - k)))
    return ema


def calculate_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


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
    print(f"\n--- Çift Yönlü Market Taraması (Filtreli) ({len(WATCHLIST) - len(active_symbols)} coin taranıyor) ---")
    
    for symbol in WATCHLIST:
        if symbol in active_symbols:
            continue

        closes, volumes = get_klines(symbol)
        if len(closes) < SLOW_EMA_PERIOD + 5 or len(volumes) < VOLUME_MA_PERIOD:
            print(f"[{symbol}] Yetersiz mum/hacim verisi")
            continue

        ema_fast = calculate_ema(closes, FAST_EMA_PERIOD)
        ema_slow = calculate_ema(closes, SLOW_EMA_PERIOD)
        current_rsi = calculate_rsi(closes)

        # Hacim Filtresi: Son mum hacmi > Son 20 mum ortalama hacmi
        avg_volume = sum(volumes[-VOLUME_MA_PERIOD:]) / VOLUME_MA_PERIOD
        current_volume = volumes[-1]
        volume_confirmed = current_volume > avg_volume

        current_price = closes[-1]
        last_fast, prev_fast = ema_fast[-1], ema_fast[-2]
        last_slow, prev_slow = ema_slow[-1], ema_slow[-2]

        is_long_cross = (prev_fast <= prev_slow) and (last_fast > last_slow)
        is_long_trend = (last_fast > last_slow) and (current_price > last_fast)

        is_short_cross = (prev_fast >= prev_slow) and (last_fast < last_slow)
        is_short_trend = (last_fast < last_slow) and (current_price < last_fast)

        # LONG Sinyali: EMA Alım + RSI Doygun Değil (<65) + Hacim Onaylı
        if (is_long_cross or is_long_trend) and current_rsi < 65 and volume_confirmed:
            score = abs((last_fast - last_slow) / last_slow) * 100
            print(f"[{symbol}] 🚀 LONG ONAYLANDI! Fiyat: {current_price} | RSI: {current_rsi} < 65 | Hacim: ONAYLI")
            candidates.append({
                "symbol": symbol,
                "price": current_price,
                "side": "BUY",
                "score": score
            })

        # SHORT Sinyali: EMA Satım + RSI Doygun Değil (>35) + Hacim Onaylı
        elif (is_short_cross or is_short_trend) and current_rsi > 35 and volume_confirmed:
            score = abs((last_fast - last_slow) / last_slow) * 100
            print(f"[{symbol}] 🔻 SHORT ONAYLANDI! Fiyat: {current_price} | RSI: {current_rsi} > 35 | Hacim: ONAYLI")
            candidates.append({
                "symbol": symbol,
                "price": current_price,
                "side": "SELL",
                "score": score
            })
        else:
            print(f"[{symbol}] Fiyat: {current_price} | RSI: {current_rsi} | EMA9: {round(last_fast, 2)} | EMA21: {round(last_slow, 2)} (Filtre Takıldı)")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def execute_order(symbol, price, side):
    balance = get_usdt_balance()
    if balance <= 10:
        print(f"[{symbol}] Yetersiz bakiye: {balance} USDT")
        return

    # Önce kaldıracı 3x yapalım
    set_leverage(symbol, TARGET_LEVERAGE)

    trade_amount_usdt = balance * ALLOCATION_PER_TRADE
    qty = round(trade_amount_usdt / price, 3)

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
        closes, _ = get_klines(symbol, limit=2)
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
            print(f"[{symbol}] LONG İzleniyor -> Güncel: {current_price} | Zirve: {highest_prices[symbol]} | Stop (%3): {round(stop_price, 4)}")

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
            print(f"[{symbol}] SHORT İzleniyor -> Güncel: {current_price} | Dip: {lowest_prices[symbol]} | Stop (%3): {round(stop_price, 4)}")

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
    print("Filtreli & Düşük Riskli Çift Yönlü Bot Başlatıldı...")
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
                    print(f"[{top_candidate['symbol']}] FİLTRELİ SİNYAL ONAYLANDI! Yön: {top_candidate['side']} İşlem Yapılıyor...")
                    execute_order(top_candidate['symbol'], top_candidate['price'], top_candidate['side'])

        except Exception as e:
            print(f"[Ana Dongu Hatasi]: {e}")

        time.sleep(10)


threading.Thread(target=bot_loop, daemon=True).start()


@app.route('/')
def home():
    active_positions = get_active_positions()
    return jsonify({
        "status": "Bi-Directional Multi-Pair Bot Active (Filtered & Low Risk)",
        "leverage": f"{TARGET_LEVERAGE}x",
        "trailing_stop": f"%{TRAILING_STOP_PERCENT * 100}",
        "active_positions": list(active_positions.keys()),
        "active_positions_count": len(active_positions),
        "max_allowed": MAX_POSITIONS
    })


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
