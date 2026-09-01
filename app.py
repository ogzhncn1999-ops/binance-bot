import os
from flask import Flask, request
from binance.client import Client

app = Flask(__name__)

api_key = os.environ.get('BINANCE_API_KEY')
api_secret = os.environ.get('BINANCE_SECRET_KEY')
client = Client(api_key, api_secret)

@app.route('/', methods=['GET'])
def index():
    return "Bot Aktif!"

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if not data:
        return {"status": "error", "message": "Veri yok"}, 400
    
    symbol = data.get('symbol')
    side = data.get('side') # BUY veya SELL
    quantity = data.get('quantity')

    try:
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type='MARKET',
            quantity=quantity
        )
        return {"status": "success", "order": order}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
