import os
import time
import hmac
import hashlib
import urllib.parse
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()

BASE_URL = "https://testnet.binancefuture.com"
INTERVAL = "1m"
TRAILING_STOP_PERCENT = 0.015  # %1.5
MAX_POSITIONS = 7               # En fazla 7 coin
ALLOCATION_PER_TRADE = 0.10     # Bakiyenin %10'u

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", 
    "XRPUSDT", "BNBUSDT", "NEARUSDT", "DOGEUSDT", "ADAUSDT"
]

highest_prices = {}

def send_signed_request(method, endpoint, params=None):
    """Binance API talepleri için doğru HMAC SHA256 imzalı istek atar."""
    if params is None:
        params = {}
    
    params["timestamp"] = int(time.time() * 1000)
    query_string = urllib.parse.urlencode(params)
    
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    full_url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    
    try:
        if method == "GET":
            response = requests.get(full_url, headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(full_url, headers=headers, timeout=10)
        return response.json()
    except Exception as e:
        print(f"[API Istek Hatasi]: {e}")
        return None

def get_usdt_balance():
    res = send_signed_request("GET", "/fapi/v2/balance")
    if isinstance(res, list):
        for item in res:
            if item.get("asset") == "USDT":
                return float(item.get("balance", 0))
    elif isinstance(res, dict) and "msg" in res:
        print(f"[Binance Bakiye Hatasi]: {res.get('msg')}")
    return 0.0

def get_open_positions():
    open_positions = {}
    res = send_signed_request("GET", "/fapi/v2/positionRisk")
    if isinstance(res, list):
        for pos in res:
            amt = float(pos.get("positionAmt", 0))
            symbol = pos.get("symbol")
            if amt != 0 and symbol in WATCHLIST:
                open_positions[symbol] = {
                    "amount": amt,
                    "entry_price": float(pos.get("entryPrice", 0))
                }
    elif isinstance(res, dict) and "msg" in res:
        print(f"[Binance Pozisyon Hatasi]: {res.get('msg')}")
    return open_positions

def get_klines(symbol):
    try:
        url = f"{BASE_URL}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": INTERVAL, "limit": 100}
        res = requests.get(url, params=params, timeout=10).json()
        if isinstance(res, list):
            return [float(item[4]) for item in res]
    except:
        pass
    return []

def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def execute_order(symbol, side, quantity):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity
    }
    res = send_signed_request("POST", "/fapi/v1/order", params)
    print(f"[{symbol}] {side} Emir Sonucu: {res}")
    return res

def analyze_opportunities(active_symbols):
    candidates = []
    for symbol in WATCHLIST:
        if symbol in active_symbols:
            continue
            
        closes = get_klines(symbol)
        if len(closes) < 50:
            continue
            
        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)
        
        current_price = closes[-1]
        last_ema20, prev_ema20 = ema20[-1], ema20[-2]
        last_ema50, prev_ema50 = ema50[-1], ema50[-2]
        
        ema_cross_up = (prev_ema20 <= prev_ema50) and (last_ema20 > last_ema50)
        
        if ema_cross_up:
            score = ((last_ema20 - last_ema50) / last_ema50) * 100
            candidates.append({
                "symbol": symbol,
                "price": current_price,
                "score": score
            })
            
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

def keep_alive():
    while True:
        try:
            time.sleep(300)
            requests.get("https://binance-bot-3u50.onrender.com", timeout=10)
        except:
            pass

def run_trading_bot():
    global highest_prices
    print("Çoklu Coin Multi-Pair Botu Baslatildi...")
    
    while True:
        try:
            open_positions = get_open_positions()
            current_active_count = len(open_positions)
            
            # 1. Trailing Stop Kontrolü
            for symbol, details in list(open_positions.items()):
                closes = get_klines(symbol)
                if not closes:
                    continue
                
                current_price = closes[-1]
                entry_price = details["entry_price"]
                
                if symbol not in highest_prices or highest_prices[symbol] < current_price:
                    highest_prices[symbol] = max(current_price, entry_price)
                
                stop_price = highest_prices[symbol] * (1 - TRAILING_STOP_PERCENT)
                
                if current_price <= stop_price:
                    print(f"[{symbol}] TRAILING STOP TETIKLENDI! Satiliyor...")
                    qty = abs(details["amount"])
                    execute_order(symbol, "SELL", qty)
                    if symbol in highest_prices:
                        del highest_prices[symbol]

            # 2. Yeni Sinyal Taraması
            if current_active_count < MAX_POSITIONS:
                opportunities = analyze_opportunities(open_positions.keys())
                
                if opportunities:
                    total_balance = get_usdt_balance()
                    if total_balance > 0:
                        trade_amount_usdt = total_balance * ALLOCATION_PER_TRADE
                        
                        for candidate in opportunities:
                            if current_active_count >= MAX_POSITIONS:
                                break
                                
                            symbol = candidate["symbol"]
                            price = candidate["price"]
                            qty = round(trade_amount_usdt / price, 3)
                            
                            if qty > 0:
                                print(f"[{symbol}] SINYAL YAKALANDI! Alim Yapiliyor...")
                                execute_order(symbol, "BUY", qty)
                                highest_prices[symbol] = price
                                current_active_count += 1

        except Exception as e:
            print(f"Bot Dongu Hatasi: {e}")
            
        time.sleep(10)

threading.Thread(target=keep_alive, daemon=True).start()
threading.Thread(target=run_trading_bot, daemon=True).start()

@app.route('/')
def home():
    open_pos = get_open_positions()
    return jsonify({
        "status": "Multi-Pair Bot Active",
        "active_positions_count": len(open_pos),
        "active_positions": list(open_pos.keys()),
        "max_allowed": MAX_POSITIONS
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
