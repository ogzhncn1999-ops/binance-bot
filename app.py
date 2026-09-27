import os
import time
import threading
from flask import Flask, render_template_string
from binance.client import Client
from binance.exceptions import BinanceAPIException

# --- BİNANCE API AYARLARI ---
API_KEY = os.environ.get("BINANCE_API_KEY", "YOUR_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "YOUR_API_SECRET")
TESTNET = os.environ.get("TESTNET", "False").lower() == "true"

client = Client(API_KEY, API_SECRET, testnet=TESTNET)

MAX_POSITIONS = 3
LEVERAGE = 5

app = Flask(__name__)

def get_usdt_balance():
    """Cüzdandaki kullanılabilir ve toplam USDT bakiyesini döner."""
    try:
        if TESTNET:
            futures_account = client.futures_account()
            total_wallet = float(futures_account.get('totalWalletBalance', 0))
            available_balance = float(futures_account.get('availableBalance', 0))
            return available_balance, total_wallet
        else:
            balance_info = client.futures_account_balance()
            for b in balance_info:
                if b['asset'] == 'USDT':
                    return float(b['availableBalance']), float(b['balance'])
        return 0.0, 0.0
    except Exception as e:
        print(f"[Bakiye Hatası]: {e}")
        return 0.0, 0.0

def get_active_positions():
    """Aktif pozisyonları sözlük formatında döner."""
    try:
        positions = client.futures_position_information()
        active = {}
        for p in positions:
            amt = float(p['positionAmt'])
            if amt != 0.0:
                symbol = p['symbol']
                active[symbol] = {
                    'positionAmt': amt,
                    'entryPrice': float(p['entryPrice']),
                    'markPrice': float(p['markPrice']),
                    'unRealizedProfit': float(p['unRealizedProfit']),
                    'leverage': int(p['leverage']),
                    'side': 'LONG' if amt > 0 else 'SHORT'
                }
        return active
    except Exception as e:
        print(f"[Pozisyon Çekme Hatası]: {e}")
        return {}

def get_detailed_positions():
    """Web Paneli için detaylı pozisyon listesi hazırlar."""
    raw_positions = get_active_positions()
    detailed = []
    for symbol, p in raw_positions.items():
        detailed.append({
            'symbol': symbol,
            'side': p['side'],
            'leverage': p['leverage'],
            'entry_price': p['entryPrice'],
            'mark_price': p['markPrice'],
            'unrealized_pnl': round(p['unRealizedProfit'], 2)
        })
    return detailed

def get_realized_pnl_summary():
    """Geçmiş kapanan işlemlerin toplam kâr/zarar (Realized PNL) özetini hesaplar."""
    total_realized = 0.0
    trades_list = []
    try:
        income_history = client.futures_income_history(incomeType='REALIZED_PNL', limit=50)
        for income in income_history:
            pnl_val = float(income['income'])
            total_realized += pnl_val
            trades_list.append({
                'symbol': income['symbol'],
                'income': round(pnl_val, 2),
                'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(income['time'])/1000))
            })
    except Exception as e:
        print(f"[Geçmiş İşlem Hatası]: {e}")
    
    return round(total_realized, 2), trades_list

def analyze_opportunities(active_symbols):
    candidates = []
    try:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        for sym in symbols:
            if sym in active_symbols:
                continue
            ticker = client.futures_symbol_ticker(symbol=sym)
            price = float(ticker['price'])
            candidates.append({
                'symbol': sym,
                'price': price,
                'side': 'LONG'
            })
    except Exception as e:
        print(f"[Analiz Hatası]: {e}")
    return candidates

def execute_order(symbol, price, side):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        avail, _ = get_usdt_balance()
        notional = avail * 0.3 
        qty = round(notional * LEVERAGE / price, 3)
        
        if qty <= 0:
            return

        order_side = client.SIDE_BUY if side == 'LONG' else client.SIDE_SELL
        client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type=client.ORDER_TYPE_MARKET,
            quantity=qty
        )
        print(f"[EMİR BAŞARILI] {symbol} {side} - Miktar: {qty}")
    except Exception as e:
        print(f"[Emir İcra Hatası] {symbol}: {e}")

def bot_loop():
    print("🤖 Binance Otomatik İşlem Botu Başlatıldı...", flush=True)
    time.sleep(5)
    while True:
        try:
            active_positions = get_active_positions()
            active_symbols = list(active_positions.keys()) if isinstance(active_positions, dict) else []

            if len(active_symbols) < MAX_POSITIONS:
                candidates = analyze_opportunities(active_symbols)
                if candidates:
                    top_candidate = candidates[0]
                    execute_order(top_candidate['symbol'], top_candidate['price'], top_candidate['side'])
        except Exception as e:
            print(f"[Ana Döngü Hatası]: {e}")

        time.sleep(120)
