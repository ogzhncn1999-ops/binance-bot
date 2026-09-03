from flask import Flask, request, jsonify
from binance.client import Client
import os
import math

app = Flask(__name__)

# Çevre Değişkenleri
API_KEY = os.environ.get('BINANCE_API_KEY')
SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY')
WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE', 'BenimGizliSifrem123')

# Binance İstemcisi
client = Client(API_KEY, SECRET_KEY)

# Risk ve Pozisyon Parametreleri
RISK_PERCENT = 0.02       # Her işlemde bakiyenin %2'si kadar risk
STOP_LOSS_PCT = 0.015    # %1.5 Stop Loss
TAKE_PROFIT_PCT = 0.030   # %3.0 Take Profit (1:2 Risk/Ödül)
LEVERAGE = 3              # 3x Kaldıraç

def set_leverage(symbol, leverage):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
    except Exception as e:
        print(f"Kaldıraç ayarlanırken hata: {e}")

def get_quantity(symbol, price, balance_pct=0.1):
    """
    Bakiyeye ve kaldıraça göre güvenli lot miktarı hesaplar.
    """
    try:
        account_info = client.futures_account()
        usdt_balance = float(account_info['availableBalance'])
        
        # Bakiyenin belirlenen yüzdesi kadar marjin kullan
        trade_amount = usdt_balance * balance_pct * LEVERAGE
        quantity = trade_amount / price
        
        # Sembol adım hassasiyetine göre yuvarlama
        return round(quantity, 3)
    except Exception as e:
        print(f"Miktar hesaplama hatası: {e}")
        return 0

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()

    if not data:
        return jsonify({"status": "error", "message": "Boş veri"}), 400

    # 1. Webhook Şifre Doğrulaması (Authentication)
    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({"status": "error", "message": "Yetkisiz Erişim!"}), 401

    symbol = data.get('symbol', 'BTCUSDT')
    side = data.get('side').upper() # 'BUY' (LONG) veya 'SELL' (SHORT)
    
    try:
        # Kaldıraç Ayarla
        set_leverage(symbol, LEVERAGE)
        
        # Güncel Fiyatı Al
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        
        # Dinamik Pozisyon Büyüklüğü Hesabı
        quantity = get_quantity(symbol, price)
        if quantity <= 0:
            return jsonify({"status": "error", "message": "Yetersiz bakiye veya hatalı miktar"}), 400

        # 2. Ana Piyasa Emrini Aç (MARKET)
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type='MARKET',
            quantity=quantity
        )

        # 3. Otomatik SL ve TP Fiyatlarını Hesapla
        if side == 'BUY':
            sl_price = round(price * (1 - STOP_LOSS_PCT), 2)
            tp_price = round(price * (1 + TAKE_PROFIT_PCT), 2)
            exit_side = 'SELL'
        else: # SELL (SHORT)
            sl_price = round(price * (1 + STOP_LOSS_PCT), 2)
            tp_price = round(price * (1 - TAKE_PROFIT_PCT), 2)
            exit_side = 'BUY'

        # 4. Binance Tarafında Gerçek Stop-Loss Emri
        client.futures_create_order(
            symbol=symbol,
            side=exit_side,
            type='STOP_MARKET',
            stopPrice=sl_price,
            closePosition=True
        )

        # 5. Binance Tarafında Gerçek Take-Profit Emri
        client.futures_create_order(
            symbol=symbol,
            side=exit_side,
            type='TAKE_PROFIT_MARKET',
            stopPrice=tp_price,
            closePosition=True
        )

        return jsonify({
            "status": "success",
            "message": f"{symbol} üzerinde {side} pozisyonu %1.5 SL ve %3.0 TP ile açıldı.",
            "entry_price": price,
            "sl_price": sl_price,
            "tp_price": tp_price
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
