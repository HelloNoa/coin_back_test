# python upbit_ai_dryrun.py
"""
업비트 AI 자동매매 봇 — 드라이런 모드
- 실제 시장 데이터를 사용하되, 주문은 실행하지 않음
- 가상 포트폴리오로 손익을 추적
- 전략 검증용

사용법:
  python upbit_ai_dryrun.py
  python upbit_ai_dryrun.py --krw 1000000    # 초기 자본금 지정 (기본 100만원)
  python upbit_ai_dryrun.py --cycles 5       # 5사이클만 실행 후 종료
"""

import os
import sys
import time
import json
import logging
import subprocess
import argparse
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import pyupbit
import pandas as pd
import requests

# upbit_ai_trader에서 공용 함수 임포트
from upbit_ai_trader import (
    get_technical_indicators,
    get_market_summary,
    ask_claude_for_decision,
    load_trade_history,
    MAX_COINS_TO_ANALYZE,
    INVEST_RATIO,
    MIN_ORDER_KRW,
)

# ──────────────────────────────────────────────
# 드라이런 설정
# ──────────────────────────────────────────────
DRYRUN_INTERVAL_SECONDS = 60 * 10   # 10분마다 (빠른 검증용)
DRYRUN_HISTORY_FILE = "dryrun_history.json"
DRYRUN_PORTFOLIO_FILE = "dryrun_portfolio.json"

# ──────────────────────────────────────────────
# 로깅
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DRYRUN] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("dryrun_log.txt", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 가상 포트폴리오 관리
# ──────────────────────────────────────────────
def load_portfolio(initial_krw: float) -> dict:
    try:
        with open(DRYRUN_PORTFOLIO_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"KRW": initial_krw, "holdings": {}, "initial_krw": initial_krw}


def save_portfolio(portfolio: dict):
    with open(DRYRUN_PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)


def get_portfolio_for_ai(portfolio: dict) -> dict:
    """AI에게 전달할 형식으로 포트폴리오 변환"""
    result = {"KRW": round(portfolio["KRW"], 0)}
    for ticker, holding in portfolio["holdings"].items():
        try:
            current_price = pyupbit.get_current_price(ticker) or 0
        except Exception:
            current_price = holding.get("last_price", 0)
        if current_price > 0:
            holding["last_price"] = current_price
        avg_price = holding["avg_buy_price"]
        amount = holding["amount"]
        result[ticker] = {
            "amount": amount,
            "avg_buy_price": avg_price,
            "current_price": current_price,
            "profit_pct": round((current_price / avg_price - 1) * 100, 2) if avg_price > 0 else 0,
            "value_krw": round(amount * current_price, 0),
        }
    return result


def calc_total_value(portfolio: dict) -> float:
    """총 자산가치 계산"""
    total = portfolio["KRW"]
    for ticker, holding in portfolio["holdings"].items():
        try:
            price = pyupbit.get_current_price(ticker) or holding.get("last_price", 0)
        except Exception:
            price = holding.get("last_price", 0)
        total += holding["amount"] * price
    return round(total, 0)


# ──────────────────────────────────────────────
# 가상 매매 실행
# ──────────────────────────────────────────────
def execute_dryrun_trade(decision: dict, portfolio: dict) -> dict | None:
    action = decision.get("action", "hold")
    ticker = decision.get("ticker", "")
    reason = decision.get("reason", "")

    if action == "buy":
        amount_krw = float(decision.get("amount_krw", 0))
        max_invest = portfolio["KRW"] * INVEST_RATIO
        amount_krw = min(amount_krw, max_invest)

        if amount_krw < MIN_ORDER_KRW:
            log.warning(f"[BUY 스킵] {ticker} 주문금액 {amount_krw:,.0f}원 < 최소금액")
            return None
        if amount_krw > portfolio["KRW"]:
            log.warning(f"[BUY 스킵] {ticker} 잔액 부족 (필요: {amount_krw:,.0f}, 보유: {portfolio['KRW']:,.0f})")
            return None

        try:
            current_price = pyupbit.get_current_price(ticker)
        except Exception:
            log.warning(f"[BUY 스킵] {ticker} 시세 조회 실패")
            return None
        if not current_price:
            return None

        buy_amount = amount_krw / current_price
        fee = amount_krw * 0.0005  # 업비트 수수료 0.05%

        # 포트폴리오 업데이트
        portfolio["KRW"] -= (amount_krw + fee)
        if ticker in portfolio["holdings"]:
            h = portfolio["holdings"][ticker]
            total_cost = h["avg_buy_price"] * h["amount"] + amount_krw
            h["amount"] += buy_amount
            h["avg_buy_price"] = total_cost / h["amount"]
            h["last_price"] = current_price
        else:
            portfolio["holdings"][ticker] = {
                "amount": buy_amount,
                "avg_buy_price": current_price,
                "last_price": current_price,
            }

        log.info(f"[BUY] {ticker} | {amount_krw:,.0f}원 | {buy_amount:.6f}개 @ {current_price:,.0f} | 수수료: {fee:,.0f}원")
        log.info(f"  └ 사유: {reason}")
        return {"action": "buy", "ticker": ticker, "amount_krw": amount_krw, "price": current_price, "qty": buy_amount}

    elif action == "sell":
        sell_ratio = float(decision.get("sell_ratio", 1.0))
        holding = portfolio["holdings"].get(ticker)
        if not holding or holding["amount"] <= 0:
            log.warning(f"[SELL 스킵] {ticker} 보유량 없음")
            return None

        try:
            current_price = pyupbit.get_current_price(ticker)
        except Exception:
            log.warning(f"[SELL 스킵] {ticker} 시세 조회 실패")
            return None
        if not current_price:
            return None

        sell_amount = holding["amount"] * sell_ratio
        sell_krw = sell_amount * current_price
        fee = sell_krw * 0.0005

        if sell_krw < MIN_ORDER_KRW:
            log.warning(f"[SELL 스킵] {ticker} 매도금액 {sell_krw:,.0f}원 < 최소금액")
            return None

        pnl_pct = round((current_price / holding["avg_buy_price"] - 1) * 100, 2)
        pnl_krw = round((current_price - holding["avg_buy_price"]) * sell_amount, 0)

        # 포트폴리오 업데이트
        portfolio["KRW"] += (sell_krw - fee)
        holding["amount"] -= sell_amount
        if holding["amount"] < 1e-8:
            del portfolio["holdings"][ticker]

        log.info(f"[SELL] {ticker} | {sell_ratio*100:.0f}% ({sell_amount:.6f}개) @ {current_price:,.0f} | 손익: {pnl_pct:+.1f}% ({pnl_krw:+,.0f}원)")
        log.info(f"  └ 사유: {reason}")
        return {"action": "sell", "ticker": ticker, "sell_krw": sell_krw, "price": current_price, "pnl_pct": pnl_pct, "pnl_krw": pnl_krw}

    return None


def save_dryrun_record(record: dict):
    try:
        with open(DRYRUN_HISTORY_FILE, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(record)
    history = history[-100:]
    with open(DRYRUN_HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="업비트 AI 드라이런")
    parser.add_argument("--krw", type=float, default=1_000_000, help="초기 자본금 (기본 100만원)")
    parser.add_argument("--cycles", type=int, default=0, help="실행 사이클 수 (0=무한)")
    parser.add_argument("--interval", type=int, default=DRYRUN_INTERVAL_SECONDS, help="사이클 간격 (초)")
    parser.add_argument("--reset", action="store_true", help="포트폴리오 초기화")
    args = parser.parse_args()

    if args.reset and os.path.exists(DRYRUN_PORTFOLIO_FILE):
        os.remove(DRYRUN_PORTFOLIO_FILE)
        log.info("드라이런 포트폴리오 초기화 완료")

    portfolio = load_portfolio(args.krw)

    log.info("=" * 50)
    log.info("업비트 AI 자동매매 봇 [드라이런 모드]")
    log.info(f"초기 자본금: {portfolio['initial_krw']:,.0f}원")
    log.info(f"현재 KRW: {portfolio['KRW']:,.0f}원 | 보유 코인: {len(portfolio['holdings'])}종")
    log.info(f"사이클 간격: {args.interval // 60}분")
    log.info("=" * 50)

    cycle = 0
    while True:
        cycle += 1
        if args.cycles > 0 and cycle > args.cycles:
            break

        try:
            log.info(f"─── 사이클 #{cycle} 시작 ───")

            # 1. 거래량 상위 코인 목록 수집
            tickers = pyupbit.get_tickers(fiat="KRW")
            volumes = []
            for i, t in enumerate(tickers):
                try:
                    info = pyupbit.get_ohlcv(t, interval="day", count=1)
                    if info is not None and not info.empty:
                        vol = info["volume"].iloc[-1] * info["close"].iloc[-1]
                        volumes.append((t, vol))
                except Exception:
                    pass
                if (i + 1) % 10 == 0:
                    time.sleep(0.5)
            volumes.sort(key=lambda x: x[1], reverse=True)
            top_tickers = [v[0] for v in volumes[:MAX_COINS_TO_ANALYZE]]
            log.info(f"분석 대상: {top_tickers}")

            # 2. 기술적 지표 수집
            indicators_list = []
            for ticker in top_tickers:
                ind = get_technical_indicators(ticker)
                if ind:
                    indicators_list.append(ind)
                time.sleep(0.1)

            # 3. 포트폴리오 (가상)
            ai_portfolio = get_portfolio_for_ai(portfolio)
            log.info(f"포트폴리오: {json.dumps(ai_portfolio, ensure_ascii=False)}")

            # 4. 시장 심리
            market_summary = get_market_summary()
            log.info(f"시장 심리: {market_summary}")

            # 5. AI 판단
            log.info("Claude CLI 판단 요청 중...")
            decisions = ask_claude_for_decision(indicators_list, ai_portfolio, market_summary)
            log.info(f"AI 결정: {decisions}")

            # 6. 가상 매매 실행
            for decision in decisions:
                if decision.get("action", "hold") == "hold":
                    log.info(f"[HOLD] {decision.get('ticker', '')} | {decision.get('reason', '')}")
                    continue
                trade_result = execute_dryrun_trade(decision, portfolio)
                if trade_result:
                    save_dryrun_record({
                        "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "cycle": cycle,
                        **trade_result,
                    })

            # 7. 성과 리포트
            save_portfolio(portfolio)
            total_value = calc_total_value(portfolio)
            total_pnl = total_value - portfolio["initial_krw"]
            total_pnl_pct = (total_value / portfolio["initial_krw"] - 1) * 100
            log.info(f"── 성과 ──")
            log.info(f"  총 자산: {total_value:,.0f}원 | 손익: {total_pnl:+,.0f}원 ({total_pnl_pct:+.2f}%)")
            log.info(f"  현금: {portfolio['KRW']:,.0f}원 | 보유: {len(portfolio['holdings'])}종")

        except Exception as e:
            log.error(f"사이클 오류: {e}", exc_info=True)
            time.sleep(60)
            continue

        if args.cycles > 0 and cycle >= args.cycles:
            break

        log.info(f"다음 사이클까지 {args.interval // 60}분 대기...\n")
        time.sleep(args.interval)

    # 최종 리포트
    log.info("=" * 50)
    log.info("드라이런 종료 — 최종 성과")
    total_value = calc_total_value(portfolio)
    total_pnl = total_value - portfolio["initial_krw"]
    total_pnl_pct = (total_value / portfolio["initial_krw"] - 1) * 100
    log.info(f"  초기 자본: {portfolio['initial_krw']:,.0f}원")
    log.info(f"  최종 자산: {total_value:,.0f}원")
    log.info(f"  총 손익: {total_pnl:+,.0f}원 ({total_pnl_pct:+.2f}%)")
    log.info(f"  총 사이클: {cycle}회")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
