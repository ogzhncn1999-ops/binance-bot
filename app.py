import os
import time
import hmac
import hashlib
import urllib.parse
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# Binance API Bilgileri (Render Environment Variables üzerinden okunur)
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

BASE_URL = "https://testnet.binancefuture.com"
INTERVAL = "1m"
TRAILING_STOP_PERCENT = 0.015  # %1.5
MAX_POSITIONS = 7               # En fazla 7 coin
ALLOCATION_PER_TRADE = 0.10     # Bakiyenin %10'u

# Taranacak Coin Takip Listesi
WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", 
    "XRPUSDT", "BNBUSDT", "NEARUSDT", "DOGEUSDT", "ADAUSDT"
]

# Pozisyonların Zirve Fiyat Takibi
highest_prices = {}

def build_signature(params):
    """Binance API talepleri için HMAC SHA256 imzası üretir."""
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return query_string, signature

def get_usdt_balance():
    """Binance Futures hesabındaki kullanılabilir USDT bakiyesini çeker."""
    try:
        url = f"{BASE_URL}/fapi/v2/balance"
        params = {"timestamp": int(time.time() * 1000)}
        query_string, signature = build_signature(params)
        full_url = f"{url}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": API_KEY}
        
        res = requests.get(full_url, headers=headers, timeout=10).json()
        for item in res:
            if item.get("asset") == "USDT":
                return float(item.get("balance", 0))
    except Exception as e:
        print(f"[HATA] Bakiye okuma hatasi: {e}")
    return 0.0

def get_open_positions():
    """Şu an açık olan pozisyonları döndürür."""
    open_positions = {}
    try:
        url = f"{BASE_URL}/fapi/v2/positionRisk"
        params = {"timestamp": int(time.time() * 1000)}
        query_string, signature = build_signature(params)
        full_url = f"{url}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": API_KEY}
        
        res = requests.get(full_url, headers=headers, timeout=10).json()
        for pos in res:
            amt = float(pos.get("positionAmt", 0))
            symbol = pos.get("symbol")
            if amt != 0 and symbol in WATCHLIST:
                open_positions[symbol] = {
                    "amount": amt,
                    "entry_price": float(pos.get("entryPrice", 0))
                }
    except Exception as e:
        print(f"[HATA] Pozisyon kontrol hatasi: {e}")
    return open_positions

def get_klines(symbol):
    """Kapanış fiyatlarını getirir."""
    try:
        url = f"{BASE_URL}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": INTERVAL, "limit": 100}
        res = requests.get(url, params=params, timeout=10).json()
        return [float(item[4]) for item in res]
    except:
        return []

def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def execute_order(symbol, side, quantity):
    """Binance Futures üzerinde Market Alım/Satım emri gönderir."""
    try:
        url = f"{BASE_URL}/fapi/v1/order"
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "timestamp": int(time.time() * 1000)
        }
        query_string, signature = build_signature(params)
        full_url = f"{url}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": API_KEY}
        
        res = requests.post(full_url, headers=headers, timeout=10).json()
        print(f"[{symbol}] {side} Emir Sonucu: {res}")
        return res
    except Exception as e:
        print(f"[{symbol}] {side} Emir Hatasi: {e}")
        return None

def analyze_opportunities(active_symbols):
    """Açık olmayan coinleri analiz eder, en kârlı/güçlü yükseliş sinyalini puanlar."""
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
        
        # EMA20, EMA50'yi yukarı kesti mi?
        ema_cross_up = (prev_ema20 <= prev_ema50) and (last_ema20 > last_ema50)
        
        if ema_cross_up:
            # Kârlılık / Momentum Puanı: EMA20'nin EMA50'ye göre uzaklık yüzdesi
            score = ((last_ema20 - last_ema50) / last_ema50) * 100
            candidates.append({
                "symbol": symbol,
                "price": current_price,
                "score": score
            })
            
    # En yüksek kârlılık potansiyeli gösteren puana göre sırala
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

def keep_alive():
    """Render'ın ücretsiz sunucusunun kapanmasını önler."""
    while True:
        try:
            time.sleep(300)
            requests.get("https://binance-bot-3u50.onrender.com", timeout=10)
            print("[Keep-Alive] Ping atildi.")
        except Exception as e:
            print(f"[Keep-Alive Hatasi]: {e}")

def run_trading_bot():
    global highest_prices
    print("Çoklu Coin Multi-Pair Botu Baslatildi...")
    
    while True:
        try:
            open_positions = get_open_positions()
            current_active_count = len(open_positions)
            
            # 1. MEVCUT POZİSYONLARIN TRAILING STOP TAKİBİ
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

            # 2. YENİ POZİSYON AÇMA KONTROLÜ (Maksimum 7 Coin)
            if current_active_count < MAX_POSITIONS:
                opportunities = analyze_opportunities(open_positions.keys())
                
                if opportunities:
                    total_balance = get_usdt_balance()
                    trade_amount_usdt = total_balance * ALLOCATION_PER_TRADE
                    
                    for candidate in opportunities:
                        if current_active_count >= MAX_POSITIONS:
                            break
                            
                        symbol = candidate["symbol"]
                        price = candidate["price"]
                        qty = round(trade_amount_usdt / price, 3)
                        
                        if qty > 0:
                            print(f"[{symbol}] EN KARLI SINYAL YAKALANDI (Puan: {candidate['score']:.2f}). Alim Yapiliyor...")
                            execute_order(symbol, "BUY", qty)
                            highest_prices[symbol] = price
                            current_active_count += 1

        except Exception as e:
            print(f"Bot Ana Dongu Hatasi: {e}")
            
        time.sleep(10)

# Kendi kendini uyandırma thread'i
threading.Thread(target=keep_alive, daemon=True).start()

# Trading Bot döngüsü
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
