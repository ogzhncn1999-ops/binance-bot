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

# --- STRATEJİ VE RİSK YÖNETİMİ AYARLARI ---
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", 
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT", 
    "MATICUSDT", "NEARUSDT", "APTUSDT", "ATOMUSDT", "FTMUSDT"
]
INTERVAL = "1h"          # 1 saatlik mum aralığı
LEVERAGE = 3             # Kaldıraç oranı
TRADE_USDT = 10.0        # Her işlem için ayrılacak marjin (USDT)

# Risk Yönetimi Limitleri (%)
STOP_LOSS_PCT = 0.025    # %2.5 zarar kes
TAKE_PROFIT_PCT = 0.05   # %5 kâr al

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
                precision = int(s['quantityPrecision'])
                symbol_precisions[sym] = precision
            log("Binance sembol hassasiyet bilgileri başarıyla yüklendi.")
    except Exception as e:
        log(f"ExchangeInfo Yükleme Hatası: {e}")

def set_leverage(symbol):
    url_path = "/fapi/v1/leverage"
    params = {"symbol": symbol, "leverage": LEVERAGE}
    send_signed_request('POST', url_path, params)

def get_klines(symbol, interval, limit=60):
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

def calculate_rsi(data, period=14):
    if len(data) < period + 1:
        return 50.0
    deltas = [data[i] - data[i-1] for i in range(1, len(data))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

def get_all_positions():
    url_path = "/fapi/v2/positionRisk"
    res = send_signed_request('GET', url_path)
    active_positions = []
    if isinstance(res, list):
        for pos in res:
            if float(pos['positionAmt']) != 0:
                active_positions.append(pos)
    return active_positions

def get_balance():
    url_path = "/fapi/v2/account"
    res = send_signed_request('GET', url_path)
    if 'assets' in res:
        for asset in res['assets']:
            if asset['asset'] == 'USDT':
                return float(asset['walletBalance']), float(asset['availableBalance'])
    return 0.0, 0.0

def format_qty(symbol, amount):
    precision = symbol_precisions.get(symbol, 3)
    formatted = f"{amount:.{precision}f}"
    if float(formatted) <= 0:
        return str(amount)
    return formatted

def close_position(symbol, pos_amt):
    side = "SELL" if float(pos_amt) > 0 else "BUY"
    qty = format_qty(symbol, abs(float(pos_amt)))
    
    url_path = "/fapi/v1/order"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"  # 5 USDT alt limit kuralına takılmadan pozisyonu kapatmayı sağlar
    }
    res = send_signed_request('POST', url_path, params)
    log(f"Risk Yönetimi Kapatma İşlemi [{symbol}]: {res}")

def open_order(symbol, side, qty):
    formatted_qty = format_qty(symbol, qty)
    
    url_path = "/fapi/v1/order"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": formatted_qty
    }
    res = send_signed_request('POST', url_path, params)
    log(f"Güçlü Analiz İşlemi Açıldı [{symbol} - {side} - Miktar: {formatted_qty}]: {res}")

def trading_bot_loop():
    log("Gelişmiş Analiz Motorlu Binance Bot Başlatıldı.")
    load_exchange_info()
    
    while True:
        try:
            active_positions = get_all_positions()
            active_symbols = [p['symbol'] for p in active_positions]
            
            # 1. Adım: Mevcut pozisyonların Stop-Loss ve Take-Profit kontrolleri
            for pos in active_positions:
                symbol = pos['symbol']
                entry_price = float(pos['entryPrice'])
                pos_amt = float(pos['positionAmt'])
                current_price = float(pos['markPrice'])
                
                if pos_amt > 0:
                    pnl_pct = (current_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - current_price) / entry_price
                
                if pnl_pct <= -STOP_LOSS_PCT:
                    log(f"Stop-Loss Tetiklendi! [{symbol}] - Zarar oranı: {pnl_pct*100:.2f}%")
                    close_position(symbol, pos_amt)
                elif pnl_pct >= TAKE_PROFIT_PCT:
                    log(f"Kâr Al Tetiklendi! [{symbol}] - Kâr oranı: {pnl_pct*100:.2f}%")
                    close_position(symbol, pos_amt)

            # 2. Adım: Gelişmiş Analiz ve Sinyal Filtreleme
            potential_signals = []
            
            for symbol in SYMBOLS:
                if symbol in active_symbols:
                    continue
                
                closes = get_klines(symbol, INTERVAL, limit=50)
                if not closes or len(closes) < 30:
                    continue
                
                ema9 = calculate_ema(closes, 9)
                ema21 = calculate_ema(closes, 21)
                ema50 = calculate_ema(closes, 50)
                rsi = calculate_rsi(closes, 14)
                
                if not ema9 or not ema21 or not ema50:
                    continue
                
                current_price = closes[-1]
                
                trend_strength = abs(ema9 - ema21) / current_price
                is_uptrend = current_price > ema50 and ema9 > ema21
                is_downtrend = current_price < ema50 and ema9 < ema21
                
                if is_uptrend and 45 < rsi < 70:
                    potential_signals.append({
                        "symbol": symbol, "side": "BUY", "strength": trend_strength, "price": current_price
                    })
                elif is_downtrend and 30 < rsi < 55:
                    potential_signals.append({
                        "symbol": symbol, "side": "SELL", "strength": trend_strength, "price": current_price
                    })
            
            potential_signals.sort(key=lambda x: x['strength'], reverse=True)
            
            wallet_balance, available_balance = get_balance()
            
            for signal in potential_signals:
                if available_balance >= TRADE_USDT:
                    symbol = signal['symbol']
                    side = signal['side']
                    current_price = signal['price']
                    
                    raw_qty = (TRADE_USDT * LEVERAGE) / current_price
                    if raw_qty > 0:
                        set_leverage(symbol)
                        open_order(symbol, side, raw_qty)
                        available_balance -= TRADE_USDT
                else:
                    break
                        
            time.sleep(300)
        except Exception as e:
            log(f"Bot Döngü Hatası: {e}")
            time.sleep(60)

@app.route('/')
def index():
    return "Gelişmiş Analiz Motorlu Binance Bot Aktif ve Çalışıyor."

if __name__ == '__main__':
    t = Thread(target=trading_bot_loop)
    t.daemon = True
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
