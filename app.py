import os
import time
import hmac
import hashlib
import requests
from flask import Flask
from urllib.parse import urlencode
from threading import Thread
import math

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
INTERVAL = "4h"          # 4 saatlik mum aralığı
LEVERAGE = 3             # Kaldıraç oranı
TRADE_USDT = 10.0        # Her işlem için ayrılacak marjin (USDT)
MAX_ACTIVE_POSITIONS = 1 # Aynı anda en fazla açılacak işlem sayısı

STOP_LOSS_PCT = 0.025    # %2.5 Zarar Kes (Dalgalanmalara karşı hafif esnetildi)
TAKE_PROFIT_PCT = 0.075  # %7.5 Kâr Al (Ödül/Risk oranı iyileştirildi)
MIN_ATR_THRESHOLD = 0.002 # Volatilite filtresi biraz daha sıkılaştırıldı

symbol_step_sizes = {}
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
    global symbol_step_sizes, symbol_precisions
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    while True:
        try:
            res = requests.get(url).json()
            if 'symbols' in res:
                for s in res['symbols']:
                    sym = s['symbol']
                    precision = int(s['quantityPrecision'])
                    symbol_precisions[sym] = precision
                    
                    for f in s['filters']:
                        if f['filterType'] == 'LOT_SIZE':
                            step_size = float(f['stepSize'])
                            symbol_step_sizes[sym] = step_size
                            break
                log("Binance sembol hassasiyet ve stepSize bilgileri başarıyla yüklendi.")
                return True
        except Exception as e:
            log(f"ExchangeInfo Yükleme Bekleniyor: {e}")
        time.sleep(3)

def set_leverage(symbol):
    url_path = "/fapi/v1/leverage"
    params = {"symbol": symbol, "leverage": LEVERAGE}
    send_signed_request('POST', url_path, params)

def get_market_data(symbol, interval, limit=60):
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        res = requests.get(url).json()
        if isinstance(res, list) and len(res) > 0:
            highs = [float(x[2]) for x in res]
            lows = [float(x[3]) for x in res]
            closes = [float(x[4]) for x in res]
            volumes = [float(x[5]) for x in res]
            return highs, lows, closes, volumes
        return [], [], [], []
    except Exception as e:
        log(f"KLINE Hatası ({symbol}): {e}")
        return [], [], [], []

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
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0
    tr_list = []
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr = max(hl, hc, lc)
        tr_list.append(tr)
    if len(tr_list) < period:
        return sum(tr_list) / len(tr_list) if tr_list else 0.0
    return sum(tr_list[-period:]) / period

def calculate_bollinger_bands(closes, period=20, std_dev=2):
    if len(closes) < period:
        return None, None, None
    sma = sum(closes[-period:]) / period
    variance = sum((x - sma) ** 2 for x in closes[-period:]) / period
    stdev = math.sqrt(variance)
    upper_band = sma + (std_dev * stdev)
    lower_band = sma - (std_dev * stdev)
    return upper_band, sma, lower_band

def calculate_macd(closes):
    if len(closes) < 35:
        return 0, 0
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    if not ema12 or not ema26:
        return 0, 0
    macd_line = ema12 - ema26
    signal_line = macd_line * 0.9  
    return macd_line, signal_line

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
    step_size = symbol_step_sizes.get(symbol, 0.001)
    precision = symbol_precisions.get(symbol, 3)
    
    precision_factor = round(1 / step_size) if step_size < 1 else 1
    rounded_amount = math.floor(amount * precision_factor) / precision_factor
    
    formatted = f"{rounded_amount:.{precision}f}"
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
        "reduceOnly": "true"
    }
    res = send_signed_request('POST', url_path, params)
    log(f"Risk Yönetimi Pozisyon Kapatma [{symbol}]: {res}")

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
    log(f"Optimize Edilmiş Güvenli İşlem Açıldı [{symbol} - {side} - Miktar: {formatted_qty}]: {res}")

def trading_bot_loop():
    log("Optimize Edilmiş 4 Saatlik Trend Botu Başlatıldı.")
    load_exchange_info()
    
    while True:
        try:
            active_positions = get_all_positions()
            active_symbols = [p['symbol'] for p in active_positions]
            
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
                    log(f"Stop-Loss Tetiklendi! [{symbol}] - Zarar: {pnl_pct*100:.2f}%")
                    close_position(symbol, pos_amt)
                elif pnl_pct >= TAKE_PROFIT_PCT:
                    log(f"Kâr Al Tetiklendi! [{symbol}] - Kâr: {pnl_pct*100:.2f}%")
                    close_position(symbol, pos_amt)

            if len(active_positions) >= MAX_ACTIVE_POSITIONS:
                time.sleep(300)
                continue

            potential_signals = []
            for symbol in SYMBOLS:
                if symbol in active_symbols:
                    continue
                
                highs, lows, closes, volumes = get_market_data(symbol, INTERVAL, limit=60)
                if not closes or len(closes) < 40:
                    continue
                
                current_price = closes[-1]
                current_volume = volumes[-1]
                avg_volume = sum(volumes[-10:]) / 10  # Son 10 mumun ortalama hacmi
                
                ema9 = calculate_ema(closes, 9)
                ema21 = calculate_ema(closes, 21)
                ema50 = calculate_ema(closes, 50)
                rsi = calculate_rsi(closes, 14)
                atr = calculate_atr(highs, lows, closes, 14)
                upper_b, mid_b, lower_b = calculate_bollinger_bands(closes, 20, 2)
                macd_line, signal_line = calculate_macd(closes)
                
                if not ema9 or not ema21 or not ema50 or not upper_b:
                    continue
                
                # Volatilite ve Hacim Filtresi (Zayıf piyasaları ele)
                if (atr / current_price) < MIN_ATR_THRESHOLD:
                    continue
                if current_volume < avg_volume * 1.1:  # Hacim ortalamanın altındaysa es geç
                    continue
                
                trend_strength = abs(ema9 - ema21) / current_price
                
                # Sıkılaştırılmış Trend Koşulları
                is_uptrend = (current_price > ema50) and (ema9 > ema21)
                macd_bullish = macd_line > signal_line
                bb_bullish = current_price > mid_b and current_price < upper_b
                
                is_downtrend = (current_price < ema50) and (ema9 < ema21)
                macd_bearish = macd_line < signal_line
                bb_bearish = current_price < mid_b and current_price > lower_b
                
                if is_uptrend and (50 < rsi < 65) and macd_bullish and bb_bullish:
                    potential_signals.append({
                        "symbol": symbol, "side": "BUY", "strength": trend_strength, "price": current_price
                    })
                elif is_downtrend and (35 < rsi < 50) and macd_bearish and bb_bearish:
                    potential_signals.append({
                        "symbol": symbol, "side": "SELL", "strength": trend_strength, "price": current_price
                    })
            
            potential_signals.sort(key=lambda x: x['strength'], reverse=True)
            wallet_balance, available_balance = get_balance()
            
            if potential_signals and len(active_positions) < MAX_ACTIVE_POSITIONS:
                signal = potential_signals[0]
                if available_balance >= TRADE_USDT:
                    symbol = signal['symbol']
                    side = signal['side']
                    current_price = signal['price']
                    
                    raw_qty = (TRADE_USDT * LEVERAGE) / current_price
                    if raw_qty > 0:
                        set_leverage(symbol)
                        open_order(symbol, side, raw_qty)
                        
            time.sleep(300)
        except Exception as e:
            log(f"Bot Döngü Hatası: {e}")
            time.sleep(60)

@app.route('/')
def index():
    return "Optimize Edilmiş Trend Botu Aktif ve Çalışıyor."

if __name__ == '__main__':
    t = Thread(target=trading_bot_loop)
    t.daemon = True
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
