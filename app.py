import os
import time
import hmac
import hashlib
import urllib.parse
import threading
import requests
from flask import Flask, render_template_string

app = Flask(__name__)

# Render Environment Variables
API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()

# --- CANLI/TESTNET ORTAM KONTROLÜ ---
TESTNET = os.environ.get("TESTNET", "False").lower() == "true"
if TESTNET:
    BASE_URL = "https://testnet.binancefuture.com"
else:
    BASE_URL = "https://fapi.binance.com"

INTERVAL = "15m"
TRAILING_STOP_PERCENT = 0.030
MAX_POSITIONS = 3
ALLOCATION_PER_TRADE = 0.60
TARGET_LEVERAGE = 3

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

highest_prices = {}  
lowest_prices = {}   


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
    params = {"symbol": symbol, "leverage": leverage}
    send_signed_request("POST", "/fapi/v1/leverage", params)


def get_exchange_rule(symbol):
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step_size = f["stepSize"]
                        precision = 0
                        if "e-" in step_size:
                            precision = int(step_size.split("e-")[1])
                        elif "." in step_size:
                            precision = len(step_size.split(".")[1].rstrip("0"))
                        return step_size, precision
    except Exception as e:
        print(f"[{symbol}] Exchange kuralı alma hatası: {e}")
    return "0.01", 2


def get_klines(symbol, limit=100):
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit={limit}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        res = response.json()
        if isinstance(res, list) and len(res) > 0:
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


def get_detailed_positions():
    res = send_signed_request("GET", "/fapi/v2/positionRisk")
    positions = []
    if isinstance(res, list):
        for pos in res:
            amt = float(pos.get("positionAmt", 0))
            if amt != 0:
                symbol = pos.get("symbol")
                entry_price = float(pos.get("entryPrice", 0))
                mark_price = float(pos.get("markPrice", 0))
                unrealized_pnl = float(pos.get("unRealizedProfit", 0))
                side = "LONG" if amt > 0 else "SHORT"
                leverage = pos.get("leverage", TARGET_LEVERAGE)
                positions.append({
                    "symbol": symbol,
                    "side": side,
                    "amount": amt,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "leverage": leverage
                })
    return positions


def get_active_positions():
    pos_list = get_detailed_positions()
    active = {}
    for p in pos_list:
        active[p["symbol"]] = {
            "amount": p["amount"],
            "entry_price": p["entry_price"],
            "side": p["side"]
        }
    return active


def get_usdt_balance():
    res = send_signed_request("GET", "/fapi/v2/account")
    if isinstance(res, dict) and "assets" in res:
        for asset in res["assets"]:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0)), float(asset.get("totalWalletBalance", 0))
    return 0.0, 0.0


def analyze_opportunities(active_symbols):
    candidates = []
    for symbol in WATCHLIST:
        if symbol in active_symbols:
            continue

        closes, volumes = get_klines(symbol)
        time.sleep(1.0)
        
        if len(closes) < 30 or len(volumes) < 20:
            continue

        ema_fast = calculate_ema(closes, FAST_EMA_PERIOD)
        ema_slow = calculate_ema(closes, SLOW_EMA_PERIOD)
        current_rsi = calculate_rsi(closes)

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

        if (is_long_cross or is_long_trend) and current_rsi < 65 and volume_confirmed:
            score = abs((last_fast - last_slow) / last_slow) * 100
            candidates.append({"symbol": symbol, "price": current_price, "side": "BUY", "score": score})

        elif (is_short_cross or is_short_trend) and current_rsi > 35 and volume_confirmed:
            score = abs((last_fast - last_slow) / last_slow) * 100
            candidates.append({"symbol": symbol, "price": current_price, "side": "SELL", "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def execute_order(symbol, price, side):
    avail_balance, _ = get_usdt_balance()
    if avail_balance < 10:
        return

    set_leverage(symbol, TARGET_LEVERAGE)

    trade_amount_usdt = avail_balance * ALLOCATION_PER_TRADE * TARGET_LEVERAGE
    if trade_amount_usdt < 22.0:
        trade_amount_usdt = 22.0

    raw_qty = (trade_amount_usdt / TARGET_LEVERAGE) / price
    _, precision = get_exchange_rule(symbol)
    qty_str = f"{raw_qty:.{precision}f}"
    qty = float(qty_str)

    if qty <= 0:
        return

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    }
    send_signed_request("POST", "/fapi/v1/order", params)
    
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
            if current_price <= stop_price:
                params = {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": abs(pos_data["amount"]), "reduceOnly": "true"}
                send_signed_request("POST", "/fapi/v1/order", params)
                if symbol in highest_prices:
                    del highest_prices[symbol]

        elif side == "SHORT":
            if symbol not in lowest_prices:
                lowest_prices[symbol] = min(pos_data["entry_price"], current_price)
            else:
                lowest_prices[symbol] = min(lowest_prices[symbol], current_price)

            stop_price = lowest_prices[symbol] * (1 + TRAILING_STOP_PERCENT)
            if current_price >= stop_price:
                params = {"symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": abs(pos_data["amount"]), "reduceOnly": "true"}
                send_signed_request("POST", "/fapi/v1/order", params)
                if symbol in lowest_prices:
                    del lowest_prices[symbol]


def bot_loop():
    time.sleep(5)
    while True:
        try:
            active_positions = get_active_positions()
            active_symbols = list(active_positions.keys()) if isinstance(active_positions, dict) else []

            if active_positions:
                manage_trailing_stops(active_positions)

            if len(active_symbols) < MAX_POSITIONS:
                candidates = analyze_opportunities(active_symbols)
                if candidates:
                    top_candidate = candidates[0]
                    execute_order(top_candidate['symbol'], top_candidate['price'], top_candidate['side'])
        except Exception as e:
            print(f"[Ana Dongu Hatasi]: {e}")

        time.sleep(120)


DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Binance Bot Canlı Takip</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: Arial, sans-serif; background-color: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: auto; background: #161b22; padding: 20px; border-radius: 10px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); }
        h1 { color: #58a6ff; text-align: center; }
        .stats { display: flex; justify-content: space-around; background: #21262d; padding: 15px; border-radius: 8px; margin-bottom: 20px; }
        .stat-box { text-align: center; }
        .stat-value { font-size: 20px; font-weight: bold; color: #f0f6fc; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 12px; text-align: center; border-bottom: 1px solid #30363d; }
        th { background-color: #21262d; color: #8b949e; }
        .long { color: #3fb950; font-weight: bold; }
        .short { color: #f85149; font-weight: bold; }
        .profit { color: #3fb950; font-weight: bold; }
        .loss { color: #f85149; font-weight: bold; }
        .footer { text-align: center; margin-top: 20px; font-size: 12px; color: #8b949e; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 Binance Otomatik İşlem & PNL Takip Paneli</h1>
        <div class="stats">
            <div class="stat-box">
                <div>Kullanılabilir Bakiye</div>
                <div class="stat-value">{{ avail_balance }} USDT</div>
            </div>
            <div class="stat-box">
                <div>Toplam Cüzdan</div>
                <div class="stat-value">{{ total_balance }} USDT</div>
            </div>
            <div class="stat-box">
                <div>Açık Pozisyon Sayısı</div>
                <div class="stat-value">{{ positions|length }} / 3</div>
            </div>
        </div>

        <h2>📊 Açık Pozisyonlar ve Kâr/Zarar (PNL)</h2>
        <table>
            <thead>
                <tr>
                    <th>Sembol</th>
                    <th>Yön</th>
                    <th>Kaldıraç</th>
                    <th>Giriş Fiyatı</th>
                    <th>Anlık Fiyat</th>
                    <th>Anlık PNL (USDT)</th>
                </tr>
            </thead>
            <tbody>
                {% if positions %}
                    {% for p in positions %}
                    <tr>
                        <td><b>{{ p.symbol }}</b></td>
                        <td class="{{ 'long' if p.side == 'LONG' else 'short' }}">{{ p.side }}</td>
                        <td>{{ p.leverage }}x</td>
                        <td>{{ p.entry_price }}</td>
                        <td>{{ p.mark_price }}</td>
                        <td class="{{ 'profit' if p.unrealized_pnl >= 0 else 'loss' }}">
                            {{ '+' if p.unrealized_pnl > 0 else '' }}{{ p.unrealized_pnl }} USDT
                        </td>
                    </tr>
                    {% endfor %}
                {% else %}
                    <tr>
                        <td colspan="6" style="color: #8b949e;">Şu anda açık pozisyon bulunmuyor. Bot sinyal bekliyor...</td>
                    </tr>
                {% endif %}
            </tbody>
        </table>
        <div class="footer">Sayfa her 30 saniyede bir otomatik yenilenir.</div>
    </div>
</body>
</html>
"""

@app.route('/')
def home():
    avail, total = get_usdt_balance()
    positions = get_detailed_positions()
    return render_template_string(
        DASHBOARD_TEMPLATE, 
        avail_balance=round(avail, 2), 
        total_balance=round(total, 2), 
        positions=positions
    )


scanner_thread = threading.Thread(target=bot_loop, daemon=True)
scanner_thread.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
