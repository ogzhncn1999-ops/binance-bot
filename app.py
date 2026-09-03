import os
import time
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# Binance Testnet API Bilgileri
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

BASE_URL = "https://testnet.binancefuture.com"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
QTY = 0.001
TRAILING_STOP_PERCENT = 0.015

in_position = False
highest_price = 0.0

def get_klines():
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 100}
    response = requests.get(url, params=params, timeout=10)
    data = response.json()
    return [float(item[4]) for item in data]

def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def run_trading_bot():
    global in_position, highest_price
    print("Otomatik Trading Botu Baslatildi...")
    
    while True:
        try:
            closes = get_klines()
            
            if len(closes) >= 50:
                ema20_list = calculate_ema(closes, 20)
                ema50_list = calculate_ema(closes, 50)
                
                current_price = closes[-1]
                last_ema20, prev_ema20 = ema20_list[-1], ema20_list[-2]
                last_ema50, prev_ema50 = ema50_list[-1], ema50_list[-2]
                
                ema_cross_up = (prev_ema20 <= prev_ema50) and (last_ema20 > last_ema50)

                if not in_position and ema_cross_up:
                    print(f"[{SYMBOL}] ALIM SINYALI! Fiyat: {current_price}")
                    in_position = True
                    highest_price = current_price

                elif in_position:
                    if current_price > highest_price:
                        highest_price = current_price
                    
                    stop_price = highest_price * (1 - TRAILING_STOP_PERCENT)
                    if current_price <= stop_price:
                        print(f"[{SYMBOL}] TRAILING STOP TETIKLENDI! Fiyat: {current_price}")
                        in_position = False
                        highest_price = 0.0

        except Exception as e:
            print(f"Bot Hatasi: {e}")
            
        time.sleep(10)

bot_thread = threading.Thread(target=run_trading_bot, daemon=True)
bot_thread.start()

@app.route('/')
def home():
    return jsonify({"status": "Bot Running", "symbol": SYMBOL, "in_position": in_position})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
