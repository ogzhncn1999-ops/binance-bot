import os
import time
import threading
from flask import Flask, jsonify
from binance.client import Client

app = Flask(__name__)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

# Binance Testnet ve Kısıtlı Bölge Aşımı için İstemci Tanımı
client = Client(
    API_KEY, 
    API_SECRET, 
    testnet=True,
    requests_params={"timeout": 10}
)
# Testnet Futures endpoint'ini doğrudan hedefleme
client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_1MINUTE
QTY = 0.001
TRAILING_STOP_PERCENT = 0.015

in_position = False
highest_price = 0.0

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
            klines = client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=100)
            closes = [float(k[4]) for k in klines]
            
            if len(closes) >= 50:
                ema20_list = calculate_ema(closes, 20)
                ema50_list = calculate_ema(closes, 50)
                
                current_price = closes[-1]
                last_ema20, prev_ema20 = ema20_list[-1], ema20_list[-2]
                last_ema50, prev_ema50 = ema50_list[-1], ema50_list[-2]
                
                ema_cross_up = (prev_ema20 <= prev_ema50) and (last_ema20 > last_ema50)

                if not in_position and ema_cross_up:
                    print(f"[{SYMBOL}] ALIM SINYALI! Fiyat: {current_price}")
                    client.futures_create_order(
                        symbol=SYMBOL, side="BUY", type="MARKET", quantity=QTY
                    )
                    in_position = True
                    highest_price = current_price

                elif in_position:
                    if current_price > highest_price:
                        highest_price = current_price
                    
                    stop_price = highest_price * (1 - TRAILING_STOP_PERCENT)
                    if current_price <= stop_price:
                        print(f"[{SYMBOL}] TRAILING STOP TETIKLENDI! Fiyat: {current_price}")
                        client.futures_create_order(
                            symbol=SYMBOL, side="SELL", type="MARKET", quantity=QTY
                        )
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
