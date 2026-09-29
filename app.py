import os
import time
import hmac
import hashlib
import requests
from flask import Flask
from urllib.parse import urlencode
from threading import Thread

app = Flask(__name__)

# --- BİNANCE API AYARLARI ---
API_KEY = os.environ.get('BINANCE_API_KEY', 'SENIN_API_KEY')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'SENIN_API_SECRET')
TESTNET = os.environ.get('TESTNET', 'False').lower() == 'true'

BASE_URL = "https://testnet.binancevision.com" if TESTNET else "https://fapi.binance.com"

# --- STRATEJİ VE BOT AYARLARI ---
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", 
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT", 
    "MATICUSDT", "NEARUSDT", "APTUSDT", "ATOMUSDT", "FTMUSDT"
]
INTERVAL = "1h"          # 1 saatlik mum aralığı
LEVERAGE = 3             # Kaldıraç oranı
TRADE_USDT = 10.0        # Her işlem için ayrılacak marjin (USDT)

symbol_precisions = {}

def log(message):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} {message}")

def send_signed_request(http_method, url_path, payload={}):
    query_string = urlencode(true_url_format(payload))
    timestamp = int(time.time() * 1000)
    query_string += f"&timestamp={timestamp}" if query_string else f"timestamp={timestamp}"
    
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    url = f"{BASE_URL}{url_path}?{query_string}&signature={signature}"
    headers = {'X-MBX-APIKEY': API_KEY}
    
    try:
        if http_method == 'GET':
            response = requests.get(url, headers=headers)
        elif http_method == 'POST':
            response = requests.post(url, headers=headers)
        elif http_method == 'DELETE':
            response = requests.delete(url, headers=headers)
        return response.json()
    except Exception as e:
        log(f"API İstek Hatası: {e}")
        return {}

def true_url_format(params):
    return {k: v for k, v in params.items() if v is not None}

def load_exchange_info():
    global symbol_precisions
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    try:
        res = requests.get(url).json()
        if 'symbols' in res:
            for s in res['symbols']:
                sym = s['symbol']
                precision = s['quantityPrecision']
                symbol_precisions[sym] = precision
            log("Binance sembol hassasiyet bilgileri başarıyla yüklendi.")
    except Exception as e:
        log(f"ExchangeInfo Yükleme Hatası: {e}")

def set_leverage(symbol):
    url_path = "/fapi/v1/leverage"
    params = {"symbol": symbol, "leverage": LEVERAGE}
    send_signed_request('POST', url_path, params)

def get_klines(symbol, interval, limit=50):
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        res = requests.get(url).json()
        if isinstance(res, list) and len(res) > 0:
            closes = [float(x[4]) for x in res]
            return closes
        return []
    except Exception as e:
        log(f"KLINE Hatası ({symbol}): {e}")
        return []

def calculate_ema(data, period):
    if not data or len(data) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def get_position(symbol):
    url_path = "/fapi/v2/positionRisk"
    res = send_signed_request('GET', url_path)
    if isinstance(res, list):
        for pos in res:
            if pos['symbol'] == symbol:
                amt = float(pos['positionAmt'])
                if amt != 0:
                    return pos
    return None

def get_balance():
    url_path = "/fapi/v2/account"
    res = send_signed_request('GET', url_path)
    if 'assets' in res:
        for asset in res['assets']:
            if asset['asset'] == 'USDT':
                return float(asset['walletBalance']), float(asset['availableBalance'])
    return 0.0, 0.0

def open_order(symbol, side, qty):
    precision = symbol_precisions.get(symbol, 3)
    formatted_qty = f"{qty:.{precision}f}"
    
    url_path = "/fapi/v1/order"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": formatted_qty
    }
    res = send_signed_request('POST', url_path, params)
    log(f"En İyi Fırsat İşleme Açıldı [{symbol} - {side} - Miktar: {formatted_qty}]: {res}")

def trading_bot_loop():
    log("Binance Akıllı Trend Botu Başlatıldı.")
    load_exchange_info()
    
    while True:
        try:
            potential_signals = []
            
            # 1. Adım: Tüm coinleri tara ve sinyalleri topla
            for symbol in SYMBOLS:
                closes = get_klines(symbol, INTERVAL, limit=30)
                if not closes or len(closes) < 21:
                    continue
                
                ema9 = calculate_ema(closes, 9)
                ema21 = calculate_ema(closes, 21)
                if not ema9 or not ema21:
                    continue
                
                # Zaten açık pozisyon varsa bu coini atla
                if get_position(symbol):
                    continue
                
                current_price = closes[-1]
                
                # Trend Gücü Hesaplama (EMA farkının fiyata oranı - potansiyel güç skoru)
                trend_strength = abs(ema9 - ema21) / current_price
                side = "BUY" if ema9 > ema21 else "SELL"
                
                potential_signals.append({
                    "symbol": symbol,
                    "side": side,
                    "strength": trend_strength,
                    "price": current_price
                })
            
            # 2. Adım: Sinyalleri trend gücüne (kâr potansiyeline) göre en yüksekten en düşüğe sırala
            potential_signals.sort(key=lambda x: x['strength'], reverse=True)
            
            # 3. Adım: Cüzdan bakiyesini kontrol et ve en güçlü sinyallere işlem aç
            wallet_balance, available_balance = get_balance()
            
            for signal in potential_signals:
                required_margin = TRADE_USDT
                
                # Eğer kullanılabilir bakiye bu işlem için yetiyorsa aç
                if available_balance >= required_margin:
                    symbol = signal['symbol']
                    side = signal['side']
                    current_price = signal['price']
                    
                    raw_qty = (TRADE_USDT * LEVERAGE) / current_price
                    if raw_qty > 0:
                        set_leverage(symbol)
                        open_order(symbol, side, raw_qty)
                        # Anlık simüle edilmiş bakiye güncellemesi (diğer döngü içi kontroller için)
                        available_balance -= required_margin
                else:
                    # Bakiye kalmadıysa diğer zayıf sinyalleri geç
                    break
                        
            time.sleep(300) # 5 dakikada bir piyasayı yeniden tarat
        except Exception as e:
            log(f"Bot Döngü Hatası: {e}")
            time.sleep(60)

@app.route('/')
def index():
    return "Binance Akıllı Trend Botu Aktif ve Çalışıyor."

if __name__ == '__main__':
    t = Thread(target=trading_bot_loop)
    t.daemon = True
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
