import os
import time
import threading
import pandas as pd
import pandas_ta as ta
from flask import Flask, jsonify
from binance.client import Client

app = Flask(__name__)

# Binance Testnet API Bilgileri (Render Environment Variables'dan okunur)
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

# Binance Client Kurulumu (Testnet)
client = Client(API_KEY, API_SECRET, testnet=True)

# Bot Ayarları
SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_1MINUTE  # 1 dakikalık mumlar
QTY = 0.001  # İşlem miktarı (BTC)
TRAILING_STOP_PERCENT = 0.015  # %1.5 Trailing Stop (Kâr Koruma)

# Takip Değişkenleri
in_position = False
highest_price = 0.0

def run_trading_bot():
    global in_position, highest_price
    print("Otomatik Trading Botu Başlatıldı...")
    
    while True:
        try:
            # 1. Binance'ten Son Mum Verilerini Çek
            klines = client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=100)
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'
            ])
            df['close'] = df['close'].astype(float)
            
            # 2. İndikatör Hesaplamaları (EMA 20 ve EMA 50)
            df['EMA20'] = ta.ema(df['close'], length=20)
            df['EMA50'] = ta.ema(df['close'], length=50)
            
            current_price = df['close'].iloc[-1]
            last_ema20 = df['EMA20'].iloc[-1]
            last_ema50 = df['EMA50'].iloc[-1]
            prev_ema20 = df['EMA20'].iloc[-2]
            prev_ema50 = df['EMA50'].iloc[-2]

            # 3. ALIM SİNYALİ (EMA20, EMA50'yi yukarı keserse)
            ema_cross_up = (prev_ema20 <= prev_ema50) and (last_ema20 > last_ema50)

            if not in_position and ema_cross_up:
                print(f"[{SYMBOL}] ALIM SİNYALİ! Fiyat: {current_price}")
                # Binance Market Buy Emri
                order = client.futures_create_order(
                    symbol=SYMBOL, side="BUY", type="MARKET", quantity=QTY
                )
                in_position = True
                highest_price = current_price
                print("BUY Emri Başarıyla İcra Edildi.")

            # 4. KÂR KORUMA & ÇIKIŞ SİNYALİ (Trailing Stop)
            elif in_position:
                # Zirve fiyatı güncelle
                if current_price > highest_price:
                    highest_price = current_price
                
                # Stop Seviyesi: Zirvenin %1.5 altı
                stop_price = highest_price * (1 - TRAILING_STOP_PERCENT)
                
                # Fiyat stop seviyesinin altına düşerse SAT
                if current_price <= stop_price:
                    print(f"[{SYMBOL}] TRAILING STOP TETİKLENDİ! Fiyat: {current_price}, Stop: {stop_price}")
                    # Binance Market Sell Emri
                    order = client.futures_create_order(
                        symbol=SYMBOL, side="SELL", type="MARKET", quantity=QTY
                    )
                    in_position = False
                    highest_price = 0.0
                    print("SELL Emri Başarıyla İcra Edildi.")

        except Exception as e:
            print(f"Bot Hatası: {e}")
            
        # 10 saniyede bir piyasayı kontrol et
        time.sleep(10)

# Botu arka planda ayrı bir thread olarak çalıştır
bot_thread = threading.Thread(target=run_trading_bot, daemon=True)
bot_thread.start()

@app.route('/')
def home():
    return jsonify({"status": "Bot Running", "symbol": SYMBOL, "in_position": in_position})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
