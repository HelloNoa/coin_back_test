import json
import random
from datetime import datetime

import numpy as np
import requests
import pandas as pd


# curl "https://api.binance.com/api/v3/klines?symbol=DOGEUSDT&interval=1d&startTime=1704067200000&limit=730" | jq '[.[].[] | select([0,1,2,3,5,6,7,8,9,10,11]|index==4)][4] | numbers' > doge_prices.json

def format_crypto_price(price, decimals=8):
    """암호화폐 가격 포맷팅 (e-표기법 제거)"""
    return f"${price:.{decimals}f}"


def get_historical_data(symbol="BTTCUSDT", interval='1m', start_year=2019, end_year=2026):
    all_data = []
    start_time = int(datetime(start_year, 1, 1).timestamp() * 1000)  # 2019-01-01
    end_time = int(datetime(end_year, 12, 31).timestamp() * 1000)
    # while start_time < int(datetime.now().timestamp() * 1000):
    while start_time < end_time:
        url = "https://api.binance.com/api/v3/klines"
        params = {
            "symbol": symbol,
            # "interval": "1d",
            # "interval": "1h",  # 👈 여기를 1d → 1h로 변경!
            "interval": interval,  # 👈 여기를 1d → 1h로 변경!
            "startTime": start_time,
            "limit": 1000  # 최대 1000일씩
        }

        data = requests.get(url, params=params).json()
        if not data:
            break

        all_data.extend(data)
        start_time = int(data[-1][6]) + 1  # 마지막 close_time + 1ms

        print(f"📥 {len(data)}개 수집됨 (총 {len(all_data)}개)")

    # DataFrame 변환
    df = pd.DataFrame(all_data, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_buy_base',
        'taker_buy_quote', 'ignore'
    ])
    df['date'] = pd.to_datetime(df['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')
    df['close_price'] = df['close'].astype(float)
    result = df[['date', 'close_price']].to_dict('records')
    save_json(result=result, filename=f"{symbol}-{interval}-{start_time}-{end_time}.json")
    return result


def save_json(result, filename):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

initial_capital, cash, position, trades = 0, 0, 0, []


def backtest_strategy_improved(data):
    print(f"📊 데이터: {len(data)}개 ({data[0]['date']} ~ {data[-1]['date']})")

    # # ===== 개선된 설정 =====
    global initial_capital, cash, position, trades
    # initial_capital = 1_000  #
    # cash = initial_capital * 0.5
    # position = (initial_capital * 0.5) / data[0]['close_price']
    # trades = []
    MAX_POSITION_RATIO = 0.8  # 최대 코인 비중 80%
    MIN_POSITION_RATIO = 0.2  # 최소 코인 비중 20%

    # 수수료 반영
    FEE_RATE = 0.001  # 0.1%

    print(f"🚀 개선 백테스트: 원금 {initial_capital:,}$")

    for i in range(1, len(data)):
        prev_price = data[i - 1]['close_price']
        curr_price = data[i]['close_price']
        pct_change = (curr_price - prev_price) / prev_price * 100

        # ===== 개선1: 거래 임계값 높임 =====
        # if abs(pct_change) < 2.0:  # 2% 이상만 거래
        #     continue
        delta = 1

        # ===== 개선2: 자본의 1% 단위 거래 =====
        # trade_amount = initial_capital * 0.01  # 자본의 1%
        # trade_amount = max(1 * abs(pct_change),initial_capital * 0.01)  # 자본의 1%
        trade_amount = max(0.5 * abs(pct_change), initial_capital * 0.01)  # 자본의 1%

        current_value = cash + position * curr_price
        current_position_ratio = (position * curr_price) / current_value

        if pct_change >= delta and position > 0 and current_position_ratio > MIN_POSITION_RATIO:
            # 매도
            sell_qty = min(position, trade_amount / curr_price)
            if sell_qty > 0:
                # ma20 = pd.Series([d['close_price'] for d in data]).rolling(20).mean()
                # if curr_price < ma20.iloc[i]:  # 하락장만 매도
                sell_value = sell_qty * curr_price * (1 - FEE_RATE)
                cash += sell_value
                position -= sell_qty
                trades.append({
                    'date': data[i]['date'],
                    'action': 'SELL',
                    'pct_change': pct_change,
                    'qty': sell_qty,
                    'price': curr_price,
                    'amount': sell_qty * curr_price,
                    'final_value': cash + position * curr_price,
                    'cash': cash,
                    'position': position
                })

        elif pct_change <= -delta and cash >= trade_amount * 1.1 and current_position_ratio < MAX_POSITION_RATIO:
            # 매수
            # ma20 = pd.Series([d['close_price'] for d in data]).rolling(20).mean()
            # if curr_price > ma20.iloc[i]:  # 상승장만 매수
            buy_qty = (trade_amount * (1 + FEE_RATE)) / curr_price
            cash -= trade_amount * (1 + FEE_RATE)
            position += buy_qty
            trades.append({
                'date': data[i]['date'],
                'action': 'BUY',
                'pct_change': pct_change,
                'qty': buy_qty,
                'price': curr_price,
                'amount': trade_amount,
                'final_value': cash + position * curr_price,
                'cash': cash,
                'position': position
            })

    final_price = data[-1]['close_price']
    final_value = cash + position * final_price
    total_return = (final_value - initial_capital) / initial_capital * 100

    print("\n" + "=" * 70)
    print("🎯 개선 백테스트 결과")
    print("=" * 70)
    print(f"📈 최종 가치:     {final_value:,.0f}$ ({total_return:+.1f}%)")
    print(f"💼 최종 현금:     {cash:,.0f}$")
    print(f"🐕 최종 도지:     {position:.0f} DOGE")
    print(f"📊 거래 횟수:     {len(trades)}회")  # 606 → 150회로 대폭 감소
    print(f"💸 바이앤홀드:    {((final_price / data[0]['close_price'] - 1) * 100):+.1f}%")
    print(f"💰🐕 시작 도지 가격:         {format_crypto_price(data[0]['close_price'])} $")
    print(f"💰🐕 최종 도지 가격:         {format_crypto_price(data[-1]['close_price'])} $")
    # 최근 거래 5건
    # print("\n📋 최근 거래:")
    # for trade in [trades[0]] + trades[-5:]:
    #     # for trade in trades:
    #     print(f"  {trade['date']}: {trade['action']} {trade['qty']:.8f} @ {trade['pct_change']:.2f}% @ ${trade['price']:.8f} | 최종가치: {trade['final_value']:,.2f}$ {trade['cash']:,.2f}$ {trade['position']:.2f}")
    return {
        'initial_capital': initial_capital,
        'final_value': final_value,
        'return_pct': total_return,
        'trades': trades,
        'final_cash': cash,
        'final_position': position
    }


if __name__ == '__main__':
    # print(get_doge_daily())
    # jsonData = json.dumps(get_doge_daily(), indent=2, ensure_ascii=False)
    # print(jsonData)
    # result = backtest_strategy()
    # symbol = "XRPUSDT"
    symbol = "DOGEUSDT"
    start_year = 2025
    end_year = 2026
    # data = get_historical_data(symbol=symbol, interval='1m', start_year=start_year, end_year=end_year)
    with open('DOGEUSDT-1m-1770081240000-1798642800000.json', 'r') as f:
        data = json.load(f)

    initial_capital = 1_00  #
    # cash = initial_capital * 0.5
    # position = (initial_capital * 0.5) / data[0]['close_price']
    cash = initial_capital
    position = 0
    trades = []

    # monte_carlo_backtest(data=data, n_simulations=10000)
    for i in range(1):
        # backtest_strategy(data=data)
        backtest_strategy_improved(data=data)
    print("\n📋 최근 거래:")
    for trade in trades:
        print(f"  {trade['date']}: {trade['action']} {trade['qty']:.8f} @ {trade['pct_change']:.2f}% @ ${trade['price']:.8f} | 최종가치: {trade['final_value']:,.2f}$ {trade['cash']:,.2f}$ {trade['position']:.2f}")
