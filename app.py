Python
from flask import Flask, request, jsonify
from binance.client import Client
import os

app = Flask(__name__)

# Çevre Değişkenleri
API_KEY = os.environ.get('BINANCE_API_KEY')
SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY')
WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE', 'BenimGizliSifrem123')

# Risk ve Pozisyon Parametreleri
RISK_PERCENT = 0.02
STOP_LOSS_PCT = 0.015
TAKE_PROFIT_PCT = 0.030
LEVERAGE = 3

def get_binance_client():
    # Bağlantıyı sadece istek geldiğinde oluşturuyoruz (bölge engelini aşmak için)
    client = Client(API_KEY, SECRET_KEY, testnet=True)
    client.API_URL = 'https://testnet.binancefuture.com/fapi'
    client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'
    return client

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "online", "message": "Binance Futures Botu Yayında!"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()

    if not data:
        return jsonify({"status": "error", "message": "Boş veri"}), 400

    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({"status": "error", "message": "Yetkisiz Erişim!"}), 401

    symbol = data.get('symbol', 'BTCUSDT')
    side = data.get('side').upper()

    try:
        client = get_binance_client()

        # Kaldıraç Ayarla
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except Exception as e:
            print(f"Kaldıraç hatası: {e}")

        # Fiyat Al
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])

        # Bakiye ve Miktar
        account_info = client.futures_account()
        usdt_balance = float(account_info['availableBalance'])
        trade_amount = usdt_balance * 0.1 * LEVERAGE
        quantity = round(trade_amount / price, 3)

        if quantity <= 0:
            return jsonify({"status": "error", "message": "Yetersiz bakiye"}), 400

        # Ana Market Emri
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type='MARKET',
            quantity=quantity
        )

        # Stop Loss ve Take Profit
        exit_side = 'SELL' if side == 'BUY' else 'BUY'
        sl_price = round(price * (1 - STOP_LOSS_PCT), 2) if side == 'BUY' else round(price * (1 + STOP_LOSS_PCT), 2)
        tp_price = round(price * (1 + TAKE_PROFIT_PCT), 2) if side == 'BUY' else round(price * (1 - TAKE_PROFIT_PCT), 2)

        client.futures_create_order(
            symbol=symbol,
            side=exit_side,
            type='STOP_MARKET',
            stopPrice=sl_price,
            closePosition=True
        )

        client.futures_create_order(
            symbol=symbol,
            side=exit_side,
            type='TAKE_PROFIT_MARKET',
            stopPrice=tp_price,
            closePosition=True
        )

        return jsonify({
            "status": "success",
            "message": f"{symbol} {side} emri açıldı.",
            "price": price,
            "sl": sl_price,
            "tp": tp_price
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

    
