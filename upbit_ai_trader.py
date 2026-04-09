# python upbit_ai_trader.py
"""
업비트 AI 자동매매 봇
- Claude Code CLI(`claude -p ...`)를 subprocess로 호출하여 매매 판단
- 완전 자동 실행

필요 패키지 설치:
  pip install pyupbit pandas requests

사전 조건:
  - Claude Code CLI 설치 및 로그인 완료 (`claude` 명령어 사용 가능 상태)
  - 업비트 API 키 환경변수 설정

사용법:
  export UPBIT_ACCESS_KEY="발급받은키"
  export UPBIT_SECRET_KEY="발급받은키"
  python upbit_ai_trader.py
"""

import os
import sys
import time
import json
import signal
import logging
import subprocess
from datetime import datetime
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
load_dotenv()

import pyupbit
import pandas as pd
import requests

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
UPBIT_ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY", "YOUR_ACCESS_KEY")
UPBIT_SECRET_KEY = os.getenv("UPBIT_SECRET_KEY", "YOUR_SECRET_KEY")

TRADE_INTERVAL_SECONDS = 60 * 30   # 30분마다 판단
RETRY_INTERVAL_SECONDS = 60        # 오류 시 1분 후 재시도
MAX_COINS_TO_ANALYZE = 10          # 분석할 코인 수 (거래량 상위)
INVEST_RATIO = 0.3                 # 1회 최대 투자 비율
MAX_CONCENTRATION = 0.3            # 단일 코인 최대 비중 (30%)
MIN_ORDER_KRW = 6000               # 업비트 최소 주문금액
STOP_LOSS_PCT = -15                # 손절 기준 (%)
TAKE_PROFIT_PCT = 30               # 익절 기준 (%)
PRICE_ALERT_PCT = 5                # 급등/급락 감지 기준 (%)
TRADE_HISTORY_FILE = "trade_history.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ──────────────────────────────────────────────
# 로깅
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            "trading_log.txt", encoding="utf-8",
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=5,             # trading_log.txt.1 ~ .5 보관
        ),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 텔레그램 알림
# ──────────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"텔레그램 전송 실패: {e}")


# ──────────────────────────────────────────────
# 거래 이력 관리
# ──────────────────────────────────────────────
def load_trade_history() -> list[dict]:
    try:
        with open(TRADE_HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_trade_record(record: dict):
    history = load_trade_history()
    history.append(record)
    # 최근 50건만 유지
    history = history[-50:]
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# 기술적 지표 계산
# ──────────────────────────────────────────────
def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 2)


def _calc_indicators(df: pd.DataFrame) -> dict:
    """단일 타임프레임에 대한 지표 계산"""
    close = df["close"]
    volume = df["volume"]

    rsi = _calc_rsi(close)

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - signal

    # 볼린저밴드 (20)
    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper_band = ma20 + 2 * std20
    lower_band = ma20 - 2 * std20

    # 거래량 지표
    vol_ma20 = volume.rolling(20).mean()
    vol_ratio = round(volume.iloc[-1] / vol_ma20.iloc[-1], 2) if vol_ma20.iloc[-1] > 0 else 0
    vol_change_pct = round((volume.iloc[-1] / volume.iloc[-2] - 1) * 100, 2) if volume.iloc[-2] > 0 else 0

    current_price = close.iloc[-1]

    return {
        "current_price": round(current_price, 2),
        "rsi": rsi,
        "macd": round(macd.iloc[-1], 4),
        "macd_signal": round(signal.iloc[-1], 4),
        "macd_histogram": round(macd_hist.iloc[-1], 4),
        "ma20": round(ma20.iloc[-1], 2),
        "upper_band": round(upper_band.iloc[-1], 2),
        "lower_band": round(lower_band.iloc[-1], 2),
        "price_vs_ma20_pct": round((current_price / ma20.iloc[-1] - 1) * 100, 2),
        "volume_ratio": vol_ratio,         # 현재 거래량 / 20기간 평균 (>1이면 거래량 급증)
        "volume_change_pct": vol_change_pct,  # 직전 대비 거래량 변화율
    }


def get_technical_indicators(ticker: str) -> dict:
    """다중 타임프레임 기술적 지표 계산 (15분/1시간/일봉)"""
    try:
        result = {"ticker": ticker}

        # 1시간봉 (메인)
        df_1h = pyupbit.get_ohlcv(ticker, interval="minute60", count=100)
        if df_1h is None or len(df_1h) < 30:
            return {}
        result["1h"] = _calc_indicators(df_1h)
        result["current_price"] = result["1h"]["current_price"]

        # 15분봉 (단기)
        time.sleep(0.1)
        df_15m = pyupbit.get_ohlcv(ticker, interval="minute15", count=100)
        if df_15m is not None and len(df_15m) >= 30:
            result["15m"] = {
                "rsi": _calc_rsi(df_15m["close"]),
                "volume_ratio": round(
                    df_15m["volume"].iloc[-1] / df_15m["volume"].rolling(20).mean().iloc[-1], 2
                ) if df_15m["volume"].rolling(20).mean().iloc[-1] > 0 else 0,
            }

        # 일봉 (장기 추세)
        time.sleep(0.1)
        df_day = pyupbit.get_ohlcv(ticker, interval="day", count=60)
        if df_day is not None and len(df_day) >= 30:
            close_d = df_day["close"]
            ma5 = close_d.rolling(5).mean()
            ma20 = close_d.rolling(20).mean()
            result["daily"] = {
                "rsi": _calc_rsi(close_d),
                "ma5": round(ma5.iloc[-1], 2),
                "ma20": round(ma20.iloc[-1], 2),
                "trend": "uptrend" if ma5.iloc[-1] > ma20.iloc[-1] else "downtrend",
                "7d_change_pct": round((close_d.iloc[-1] / close_d.iloc[-7] - 1) * 100, 2) if len(close_d) >= 7 else 0,
                "30d_change_pct": round((close_d.iloc[-1] / close_d.iloc[-30] - 1) * 100, 2) if len(close_d) >= 30 else 0,
            }

        return result
    except Exception as e:
        log.warning(f"지표 계산 실패 ({ticker}): {e}")
        return {}


# ──────────────────────────────────────────────
# 시장 심리 수집
# ──────────────────────────────────────────────
def get_kimchi_premium() -> str:
    """김치 프리미엄 계산 (업비트 BTC 가격 vs 바이낸스 BTC 가격)"""
    try:
        # 업비트 BTC/KRW
        upbit_btc = pyupbit.get_current_price("KRW-BTC")
        # 업비트 USDT/KRW (환율 대용)
        usdt_krw = pyupbit.get_current_price("KRW-USDT")
        # 바이낸스 BTC/USDT
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=5,
        )
        binance_btc_usdt = float(resp.json()["price"])
        # 바이낸스 가격을 KRW로 환산
        binance_btc_krw = binance_btc_usdt * usdt_krw
        premium = round((upbit_btc / binance_btc_krw - 1) * 100, 2)
        return f"김치프리미엄: {premium}%"
    except Exception:
        return "김치프리미엄: 조회 실패"


def get_market_summary() -> str:
    """공포/탐욕 지수 + 김치 프리미엄 조회"""
    parts = []
    # 공포/탐욕 지수
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1", timeout=5
        )
        fng = resp.json()["data"][0]
        parts.append(f"공포탐욕지수: {fng['value']} ({fng['value_classification']})")
    except Exception:
        parts.append("공포탐욕지수: 조회 실패")
    # 김치 프리미엄
    parts.append(get_kimchi_premium())
    return " | ".join(parts)


# ──────────────────────────────────────────────
# AI 판단 요청 (Claude Code CLI 사용)
# ──────────────────────────────────────────────
def ask_claude_for_decision(
    indicators_list: list[dict],
    portfolio: dict,
    market_summary: str,
) -> dict:
    """
    Claude Code CLI를 subprocess로 호출하여 매매 결정을 JSON 배열로 받음.
    반환: list[dict] — 각 항목은 buy/sell/hold 액션
    """
    trade_history = load_trade_history()
    recent_history = trade_history[-10:] if trade_history else []

    prompt = f"""당신은 암호화폐 퀀트 트레이더입니다.
주어진 기술적 지표, 포트폴리오 현황, 시장 심리 데이터를 분석하여
최적의 매매 결정을 내려야 합니다.

반드시 아래 JSON 배열 형식으로만 응답하세요 (다른 텍스트 없이, 마크다운 없이).
여러 코인에 대해 동시에 판단할 수 있습니다. 아무 행동도 필요 없으면 빈 배열 []을 반환하세요.

[
  {{
    "action": "buy" 또는 "sell" 또는 "hold",
    "ticker": "KRW-XXX",
    "amount_krw": 숫자 (매수 시),
    "sell_ratio": 0~1 사이 숫자 (매도 시),
    "reason": "한 문장 판단 근거"
  }}
]

판단 기준:
- RSI < 30: 과매도 (매수 고려), RSI > 70: 과매수 (매도 고려)
- 다중 타임프레임: 15분/1시간/일봉 RSI가 모두 같은 방향이면 신호 강도 높음
- MACD > Signal: 상승 모멘텀
- 가격이 하단 볼린저밴드 근처: 반등 가능성
- 거래량: volume_ratio > 2이면 거래량 급증 (추세 전환 가능), < 0.5면 관심 저조
- 일봉 trend가 downtrend인데 단기 매수는 위험 (역추세 매매 주의)
- 공포탐욕지수 < 25: 극단적 공포 (매수 기회)
- 공포탐욕지수 > 75: 극단적 탐욕 (매도 고려)
- 김치프리미엄 > 5%: 국내 과열, 매도 고려. < -2%: 저평가, 매수 기회
- 손절: 보유 코인이 {STOP_LOSS_PCT}% 이하 손실이면 매도 적극 고려
- 익절: 보유 코인이 +{TAKE_PROFIT_PCT}% 이상 수익이면 일부 매도 고려
- 리스크 관리: 단일 거래에 전체 자산의 30% 이상 투자 금지

현재 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}
시장 심리: {market_summary}

포트폴리오:
{json.dumps(portfolio, ensure_ascii=False, indent=2)}

기술적 지표 (상위 거래량 코인):
{json.dumps(indicators_list, ensure_ascii=False, indent=2)}

최근 거래 이력 (참고용):
{json.dumps(recent_history, ensure_ascii=False, indent=2) if recent_history else "없음"}

위 데이터를 종합하여 최적의 매매 결정을 JSON 배열로만 응답하세요."""

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text", "--model", "sonnet"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"claude CLI 오류 (code={result.returncode}): {error_msg}")

    raw = result.stdout.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        decision = json.loads(raw)
    except json.JSONDecodeError:
        log.error(f"AI 응답 JSON 파싱 실패: {raw[:300]}")
        return []
    # 단일 dict면 배열로 감싸기 (하위 호환)
    if isinstance(decision, dict):
        decision = [decision]
    return decision


# ──────────────────────────────────────────────
# 포트폴리오 조회
# ──────────────────────────────────────────────
def get_portfolio(upbit) -> dict:
    for attempt in range(3):
        try:
            balances = upbit.get_balances()
            break
        except Exception as e:
            if attempt < 2:
                logging.warning(f"잔고 조회 실패 (시도 {attempt+1}/3): {e}")
                time.sleep(1)
            else:
                raise
    portfolio = {}
    for b in balances:
        currency = b["currency"]
        amount = float(b["balance"])
        if amount > 0:
            if currency == "KRW":
                portfolio["KRW"] = round(amount, 0)
            else:
                avg_price = float(b.get("avg_buy_price", 0))
                try:
                    current_price = pyupbit.get_current_price(f"KRW-{currency}") or 0
                except Exception:
                    log.warning(f"KRW-{currency} 시세 조회 실패, 건너뜀")
                    continue
                portfolio[f"KRW-{currency}"] = {
                    "amount": amount,
                    "avg_buy_price": avg_price,
                    "current_price": current_price,
                    "profit_pct": round((current_price / avg_price - 1) * 100, 2) if avg_price > 0 else 0,
                    "value_krw": round(amount * current_price, 0),
                }
    return portfolio


# ──────────────────────────────────────────────
# AI 판단 검증
# ──────────────────────────────────────────────
def validate_decisions(decisions: list[dict], portfolio: dict, valid_tickers: set) -> list[dict]:
    """AI 응답을 검증하여 유효한 결정만 반환"""
    validated = []
    for d in decisions:
        action = d.get("action", "")
        ticker = d.get("ticker", "")

        # 액션 검증
        if action not in ("buy", "sell", "hold"):
            log.warning(f"[검증 실패] 잘못된 action: {action}")
            continue

        if action == "hold":
            validated.append(d)
            continue

        # 티커 검증
        if ticker not in valid_tickers:
            log.warning(f"[검증 실패] 존재하지 않는 티커: {ticker}")
            continue

        # 매도 검증: 보유 여부
        if action == "sell" and ticker not in portfolio:
            log.warning(f"[검증 실패] {ticker} 미보유 상태에서 매도 시도")
            continue

        # sell_ratio 범위 검증
        if action == "sell":
            ratio = float(d.get("sell_ratio", 1.0))
            if ratio <= 0 or ratio > 1:
                log.warning(f"[검증 실패] {ticker} sell_ratio 범위 초과: {ratio}")
                continue

        # amount_krw 검증
        if action == "buy":
            amount = float(d.get("amount_krw", 0))
            if amount <= 0:
                log.warning(f"[검증 실패] {ticker} amount_krw 잘못됨: {amount}")
                continue

        validated.append(d)

    return validated


# ──────────────────────────────────────────────
# 포트폴리오 집중도 체크
# ──────────────────────────────────────────────
def check_concentration(ticker: str, buy_amount_krw: float, portfolio: dict) -> bool:
    """단일 코인 비중이 MAX_CONCENTRATION을 초과하는지 확인"""
    # 총 자산 계산
    total_value = portfolio.get("KRW", 0)
    for t, info in portfolio.items():
        if t == "KRW":
            continue
        total_value += info.get("value_krw", 0)

    if total_value <= 0:
        return True

    # 해당 코인의 현재 가치 + 추가 매수액
    current_value = 0
    if ticker in portfolio and ticker != "KRW":
        current_value = portfolio[ticker].get("value_krw", 0)
    new_concentration = (current_value + buy_amount_krw) / (total_value + buy_amount_krw)

    if new_concentration > MAX_CONCENTRATION:
        log.warning(f"[집중도 초과] {ticker} 매수 후 비중 {new_concentration*100:.1f}% > {MAX_CONCENTRATION*100:.0f}% 제한")
        return False
    return True


# ──────────────────────────────────────────────
# 매매 실행
# ──────────────────────────────────────────────
def execute_trade(upbit, decision: dict, portfolio: dict):
    action = decision.get("action", "hold")
    ticker = decision.get("ticker", "")
    reason = decision.get("reason", "")

    if action == "buy":
        krw_balance = portfolio.get("KRW", 0)
        amount_krw = float(decision.get("amount_krw", 0))
        max_invest = krw_balance * INVEST_RATIO
        amount_krw = min(amount_krw, max_invest)

        if not check_concentration(ticker, amount_krw, portfolio):
            return

        if amount_krw < MIN_ORDER_KRW:
            log.warning(f"[BUY 취소] 주문금액 {amount_krw:,.0f}원이 최소금액 미달")
            return

        log.info(f"[BUY] {ticker} {amount_krw:,.0f}원 매수 시도 | 이유: {reason}")
        result = upbit.buy_market_order(ticker, amount_krw)
        log.info(f"[BUY 주문] {result}")
        # 체결 확인
        if isinstance(result, dict) and "uuid" in result:
            time.sleep(1)
            order = upbit.get_order(result["uuid"])
            if order and "trades" in order:
                trades = order["trades"]
                filled_qty = sum(float(t["volume"]) for t in trades)
                avg_price = sum(float(t["price"]) * float(t["volume"]) for t in trades) / filled_qty if filled_qty else 0
                fee = float(order.get("paid_fee", 0))
                log.info(f"[BUY 체결] {ticker} {filled_qty:.6f}개 @ 평균 {avg_price:,.0f}원 | 수수료: {fee:,.0f}원")
                send_telegram(f"🟢 *매수 체결* | {ticker}\n{filled_qty:.6f}개 @ {avg_price:,.0f}원\n수수료: {fee:,.0f}원\n사유: {reason}")
            else:
                send_telegram(f"🟢 *매수* | {ticker}\n금액: {amount_krw:,.0f}원\n사유: {reason}")
        else:
            log.warning(f"[BUY 주문 실패] {result}")
            send_telegram(f"⚠️ *매수 주문 실패* | {ticker}\n{result}")

    elif action == "sell":
        sell_ratio = float(decision.get("sell_ratio", 1.0))
        coin_info = portfolio.get(ticker)
        if not coin_info:
            log.warning(f"[SELL 취소] {ticker} 보유량 없음")
            return

        sell_amount = coin_info["amount"] * sell_ratio
        if sell_amount * coin_info["current_price"] < MIN_ORDER_KRW:
            log.warning(f"[SELL 취소] 매도 금액이 최소금액 미달")
            return

        log.info(f"[SELL] {ticker} {sell_ratio*100:.0f}% ({sell_amount:.6f}) 매도 시도 | 이유: {reason}")
        result = upbit.sell_market_order(ticker, sell_amount)
        log.info(f"[SELL 주문] {result}")
        pnl = coin_info["profit_pct"]
        # 체결 확인
        if isinstance(result, dict) and "uuid" in result:
            time.sleep(1)
            order = upbit.get_order(result["uuid"])
            if order and "trades" in order:
                trades = order["trades"]
                filled_qty = sum(float(t["volume"]) for t in trades)
                avg_price = sum(float(t["price"]) * float(t["volume"]) for t in trades) / filled_qty if filled_qty else 0
                total_krw = round(filled_qty * avg_price, 0)
                fee = float(order.get("paid_fee", 0))
                log.info(f"[SELL 체결] {ticker} {filled_qty:.6f}개 @ 평균 {avg_price:,.0f}원 = {total_krw:,.0f}원 | 수수료: {fee:,.0f}원")
                send_telegram(f"🔴 *매도 체결* | {ticker}\n{filled_qty:.6f}개 @ {avg_price:,.0f}원\n금액: {total_krw:,.0f}원 | 손익: {pnl:+.1f}%\n수수료: {fee:,.0f}원\n사유: {reason}")
            else:
                send_telegram(f"🔴 *매도* | {ticker}\n비율: {sell_ratio*100:.0f}% | 손익: {pnl:+.1f}%\n사유: {reason}")
        else:
            log.warning(f"[SELL 주문 실패] {result}")
            send_telegram(f"⚠️ *매도 주문 실패* | {ticker}\n{result}")


# ──────────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────────
_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.info("종료 신호 수신, 현재 사이클 완료 후 종료합니다...")


def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log.info("=" * 50)
    log.info("업비트 AI 자동매매 봇 시작")
    log.info("=" * 50)
    send_telegram("🤖 *업비트 AI 자동매매 봇 시작*")

    upbit = pyupbit.Upbit(UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)

    while not _shutdown_requested:
        try:
            log.info("─── 분석 사이클 시작 ───")

            # 1. 거래량 상위 코인 목록 수집 (전체 시세 API 1회 호출)
            all_tickers = pyupbit.get_tickers(fiat="KRW")
            valid_tickers = set(all_tickers)
            try:
                ticker_info = requests.get(
                    "https://api.upbit.com/v1/ticker",
                    params={"markets": ",".join(all_tickers)},
                    timeout=10,
                ).json()
                volumes = [(t["market"], t["acc_trade_price_24h"]) for t in ticker_info]
                volumes.sort(key=lambda x: x[1], reverse=True)
            except Exception as e:
                log.warning(f"전체 시세 조회 실패, 개별 조회로 전환: {e}")
                volumes = []
                for i, t in enumerate(all_tickers):
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

            # 2. 포트폴리오 조회
            time.sleep(1)
            portfolio = get_portfolio(upbit)
            log.info(f"현재 포트폴리오: {portfolio}")

            # 보유 코인도 분석 대상에 포함
            holding_tickers = [t for t in portfolio if t != "KRW" and t not in top_tickers]
            analyze_tickers = top_tickers + holding_tickers
            log.info(f"분석 대상 코인: {analyze_tickers}")

            # 3. 기술적 지표 수집
            indicators_list = []
            for ticker in analyze_tickers:
                ind = get_technical_indicators(ticker)
                if ind:
                    indicators_list.append(ind)
                time.sleep(0.1)

            # 4. 시장 심리 수집
            market_summary = get_market_summary()
            log.info(f"시장 심리: {market_summary}")

            # 5. AI 판단 (Claude Code CLI)
            log.info("Claude CLI에게 판단 요청 중...")
            decisions = ask_claude_for_decision(indicators_list, portfolio, market_summary)
            log.info(f"AI 결정 (원본): {decisions}")

            # 6. 검증
            decisions = validate_decisions(decisions, portfolio, valid_tickers)
            log.info(f"AI 결정 (검증 후): {decisions}")

            # 7. 매매 실행
            for decision in decisions:
                if decision.get("action", "hold") == "hold":
                    log.info(f"[HOLD] {decision.get('ticker', '')} | {decision.get('reason', '')}")
                    continue
                execute_trade(upbit, decision, portfolio)
                save_trade_record({
                    "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                    **decision,
                })

        except Exception as e:
            log.error(f"사이클 오류: {e}", exc_info=True)
            log.info(f"오류 발생, {RETRY_INTERVAL_SECONDS}초 후 재시도...")
            time.sleep(RETRY_INTERVAL_SECONDS)
            continue

        log.info(f"다음 분석까지 {TRADE_INTERVAL_SECONDS//60}분 대기 (급변동 감지 중)...\n")
        if not wait_with_alert(upbit, portfolio):
            log.info("급변동 감지! 즉시 사이클 재실행")

    # 종료 처리
    log.info("=" * 50)
    log.info("봇 안전 종료 완료")
    log.info("=" * 50)
    send_telegram("🛑 *업비트 AI 자동매매 봇 종료*")


def wait_with_alert(upbit, portfolio: dict) -> bool:
    """대기 중 급등/급락 감지. 정상 완료 시 True, 급변동 감지 시 False 반환."""
    # 모니터링 대상: 보유 코인 + BTC
    watch_tickers = [t for t in portfolio if t != "KRW"]
    if "KRW-BTC" not in watch_tickers:
        watch_tickers.append("KRW-BTC")

    # 기준 가격 저장
    base_prices = {}
    for t in watch_tickers:
        try:
            price = pyupbit.get_current_price(t)
            if price:
                base_prices[t] = price
        except Exception:
            pass

    if not base_prices:
        time.sleep(TRADE_INTERVAL_SECONDS)
        return True

    elapsed = 0
    check_interval = 60  # 1분마다 체크
    last_report_date = datetime.now().strftime('%Y-%m-%d')

    while elapsed < TRADE_INTERVAL_SECONDS:
        if _shutdown_requested:
            return True

        time.sleep(check_interval)
        elapsed += check_interval

        # 급변동 체크
        for t, base_price in base_prices.items():
            try:
                current = pyupbit.get_current_price(t)
                if not current:
                    continue
                change_pct = (current / base_price - 1) * 100
                if abs(change_pct) >= PRICE_ALERT_PCT:
                    direction = "급등" if change_pct > 0 else "급락"
                    log.warning(f"[{direction}] {t} {change_pct:+.1f}% ({base_price:,.0f} → {current:,.0f})")
                    send_telegram(f"⚡ *{direction} 감지* | {t}\n변동: {change_pct:+.1f}%\n{base_price:,.0f} → {current:,.0f}원")
                    return False
            except Exception:
                pass

        # 일일 성과 리포트 (자정 직후 1회)
        now_date = datetime.now().strftime('%Y-%m-%d')
        if now_date != last_report_date and datetime.now().hour == 0:
            last_report_date = now_date
            send_daily_report(upbit)

    return True


def send_daily_report(upbit):
    """일일 거래 요약 + 총 자산 현황을 텔레그램으로 전송"""
    try:
        portfolio = get_portfolio(upbit)
        total_value = portfolio.get("KRW", 0)
        holdings_text = []
        for t, info in portfolio.items():
            if t == "KRW":
                continue
            total_value += info["value_krw"]
            holdings_text.append(f"  {t}: {info['value_krw']:,.0f}원 ({info['profit_pct']:+.1f}%)")

        # 당일 거래 이력
        history = load_trade_history()
        today = datetime.now().strftime('%Y-%m-%d')
        today_trades = [h for h in history if h.get("time", "").startswith(today)]

        report = f"📊 *일일 리포트* ({today})\n"
        report += f"총 자산: {total_value:,.0f}원\n"
        report += f"현금: {portfolio.get('KRW', 0):,.0f}원\n"
        if holdings_text:
            report += "보유:\n" + "\n".join(holdings_text) + "\n"
        report += f"당일 거래: {len(today_trades)}건"

        log.info(report)
        send_telegram(report)
    except Exception as e:
        log.warning(f"일일 리포트 생성 실패: {e}")


if __name__ == "__main__":
    main()