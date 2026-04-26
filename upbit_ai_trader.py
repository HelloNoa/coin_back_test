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
import time
import json
import signal
import logging
import subprocess
import threading
from datetime import datetime, timedelta
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

TRADE_INTERVAL_SECONDS = 60 * 60   # 1시간마다 판단
RETRY_INTERVAL_SECONDS = 60        # 오류 시 1분 후 재시도
MAX_COINS_TO_ANALYZE = 10          # 분석할 코인 수 (거래량 상위)
INVEST_RATIO = 0.3                 # 1회 최대 투자 비율
MAX_INVEST_KRW = 500_000           # 1회 최대 투자 금액 (50만원)
MAX_CONCENTRATION = 0.3            # 단일 코인 최대 비중 (30%)
MAX_POSITIONS = 5                  # 보유 코인 최대 개수 (sellable만 카운트)
MIN_KRW_RESERVE = 50_000           # 매수 후 남아야 할 최소 KRW 잔고
MIN_ORDER_KRW = 6000               # 업비트 최소 주문금액
STOP_LOSS_PCT = -15                # 손절 기준 (%)
TAKE_PROFIT_PCT = 30               # 익절 기준 (%)
PRICE_ALERT_PCT = 5                # 급등/급락 감지 기준 (%)
SPLIT_ORDER_COUNT = 3              # 분할 매매 횟수
SPLIT_ORDER_DELAY = 5              # 분할 주문 간 대기 (초)
MIN_SPLIT_KRW = 100_000            # 이 금액 이상일 때만 분할 매매
ALERT_COOLDOWN_SECONDS = 60 * 5    # 급변동 감지 후 최소 5분 대기
CLAUDE_MODEL = "sonnet"            # AI 판단 모델 (sonnet, haiku, opus 또는 전체 모델명)
TRADE_COOLDOWN_HOURS = 2           # 같은 코인 매매 후 N시간 쿨다운
TRAILING_STOP_PCT = -10            # 피크 대비 -N% 빠지면 트레일링 손절 신호
ERROR_ALERT_INTERVAL = 60 * 30     # 에러 텔레그램 알림 최소 간격 (30분)
EVAL_MIN_HOURS = 4                 # 과거 판단 평가 시 최소 경과 시간 (단기 noise 차단)
EVAL_NOISE_PCT = 1.0               # ±N% 미만 변동은 noise로 분류 (수수료/잔변동 흡수)
STABLECOINS = {"KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-TUSD"}
TRADE_HISTORY_FILE = "trade_history.json"
TOKEN_USAGE_FILE = "token_usage.json"
LAST_REPORT_FILE = "last_report.txt"
PEAKS_FILE = "position_peaks.json"

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
def _md_escape(text) -> str:
    """Telegram legacy Markdown 특수문자 이스케이프."""
    if text is None:
        return ""
    s = str(text)
    for ch in ("_", "*", "[", "`"):
        s = s.replace(ch, "\\" + ch)
    return s


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


def _load_token_usage() -> dict:
    try:
        with open(TOKEN_USAGE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_token_usage(input_tok: int, output_tok: int, cost: float):
    usage = _load_token_usage()
    today = datetime.now().strftime('%Y-%m-%d')
    if today not in usage:
        usage[today] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    usage[today]["input_tokens"] += input_tok
    usage[today]["output_tokens"] += output_tok
    usage[today]["cost_usd"] += cost
    usage[today]["calls"] += 1
    # 30일 이상 된 데이터 정리
    cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    usage = {k: v for k, v in usage.items() if k >= cutoff}
    with open(TOKEN_USAGE_FILE, "w") as f:
        json.dump(usage, f, indent=2)


def _load_peaks() -> dict:
    try:
        with open(PEAKS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_peaks(peaks: dict):
    try:
        with open(PEAKS_FILE, "w") as f:
            json.dump(peaks, f, indent=2)
    except Exception as e:
        log.warning(f"peak 저장 실패: {e}")


def update_peaks(portfolio: dict) -> dict:
    """보유 코인의 peak 가격 업데이트. 미보유 코인은 제거. 갱신된 peaks dict 반환."""
    peaks = _load_peaks()
    active_tickers = {
        t for t, info in portfolio.items()
        if t != "KRW" and isinstance(info, dict) and info.get("current_price", 0) > 0
    }
    peaks = {t: v for t, v in peaks.items() if t in active_tickers}
    for t in active_tickers:
        current = portfolio[t]["current_price"]
        if current > peaks.get(t, 0):
            peaks[t] = current
    _save_peaks(peaks)
    return peaks


def annotate_peaks(portfolio: dict) -> dict:
    """update_peaks 호출 + portfolio 각 코인에 peak_price/drawdown_from_peak_pct 주입."""
    peaks = update_peaks(portfolio)
    for t, info in portfolio.items():
        if t == "KRW" or not isinstance(info, dict):
            continue
        peak = peaks.get(t, info.get("current_price", 0))
        info["peak_price"] = round(peak, 2)
        if peak > 0:
            info["drawdown_from_peak_pct"] = round((info["current_price"] / peak - 1) * 100, 2)
    return peaks


def _is_in_cooldown(ticker: str) -> bool:
    """최근 TRADE_COOLDOWN_HOURS 시간 내 같은 티커 거래가 있으면 True."""
    history = load_trade_history()
    cutoff = datetime.now() - timedelta(hours=TRADE_COOLDOWN_HOURS)
    for record in reversed(history):
        if record.get("ticker") != ticker:
            continue
        try:
            t = datetime.strptime(record.get("time", ""), "%Y-%m-%d %H:%M")
            if t >= cutoff:
                return True
        except ValueError:
            continue
    return False


def evaluate_past_decisions() -> str:
    """과거 매매 판단의 정확도를 현재 가격과 비교하여 요약.
    - 거래 후 EVAL_MIN_HOURS 미만은 단기 noise로 보고 평가 제외
    - |change| < EVAL_NOISE_PCT 는 ≈ (의미 없는 변동)으로 분류
    """
    history = load_trade_history()
    if not history:
        return "없음"

    cutoff = datetime.now() - timedelta(hours=EVAL_MIN_HOURS)
    eligible = []
    for r in history[-30:]:
        action = r.get("action")
        if action not in ("buy", "sell"):
            continue
        if not r.get("ticker") or not r.get("trade_price"):
            continue
        try:
            t = datetime.strptime(r.get("time", ""), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if t > cutoff:  # 너무 최근 — 평가 보류
            continue
        eligible.append(r)

    recent = eligible[-10:]
    if not recent:
        return "평가 가능한 거래 없음 (최소 {}시간 경과 필요)".format(EVAL_MIN_HOURS)

    tickers_to_check = list({r["ticker"] for r in recent})
    prices = {}
    try:
        price_result = pyupbit.get_current_price(tickers_to_check)
        if isinstance(price_result, dict):
            prices = price_result
        elif isinstance(price_result, (int, float)) and len(tickers_to_check) == 1:
            prices = {tickers_to_check[0]: price_result}
    except Exception:
        pass

    evaluations = []
    for record in recent:
        ticker = record["ticker"]
        action = record["action"]
        trade_price = record["trade_price"]
        current_price = prices.get(ticker)
        if not current_price:
            continue
        change_pct = round((current_price / trade_price - 1) * 100, 2)
        if abs(change_pct) < EVAL_NOISE_PCT:
            verdict = "≈"  # 의미 없는 변동
        elif (action == "buy" and change_pct > 0) or (action == "sell" and change_pct < 0):
            verdict = "✓"
        else:
            verdict = "✗"
        evaluations.append(
            f"{record.get('time', '?')} {action.upper()} {ticker} @ {trade_price:,.0f} → 현재 {current_price:,.0f} ({change_pct:+.1f}%) {verdict}"
        )

    return "\n".join(evaluations) if evaluations else "없음"


# ──────────────────────────────────────────────
# 기술적 지표 계산
# ──────────────────────────────────────────────
def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder smoothing RSI (alpha=1/period). 차트 도구 대부분의 표준."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
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
# 오더북 분석
# ──────────────────────────────────────────────
def get_orderbook_summary(ticker: str) -> dict:
    """호가 데이터에서 매수/매도 압력 요약"""
    try:
        orderbook = pyupbit.get_orderbook(ticker)
        if not orderbook or not isinstance(orderbook, list):
            return {}
        units = orderbook[0].get("orderbook_units", [])
        if not units:
            return {}

        bid_total = sum(u["bid_size"] * u["bid_price"] for u in units)  # 매수 대기 금액
        ask_total = sum(u["ask_size"] * u["ask_price"] for u in units)  # 매도 대기 금액
        pressure = round(bid_total / ask_total, 2) if ask_total > 0 else 0
        spread = round((units[0]["ask_price"] / units[0]["bid_price"] - 1) * 100, 4)

        return {
            "bid_total_krw": round(bid_total, 0),
            "ask_total_krw": round(ask_total, 0),
            "buy_pressure": pressure,  # >1이면 매수 우세, <1이면 매도 우세
            "spread_pct": spread,
        }
    except Exception:
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
_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
                    "ticker": {"type": "string"},
                    "amount_krw": {"type": "number"},
                    "sell_ratio": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "ticker", "reason"],
            },
        }
    },
    "required": ["decisions"],
}


_SYSTEM_PROMPT = f"""당신은 암호화폐 퀀트 트레이더입니다.
주어진 기술적 지표, 포트폴리오 현황, 시장 심리 데이터를 분석하여
최적의 매매 결정을 내려야 합니다.

반드시 아래 JSON 객체 형식으로만 응답하세요.
**중요: 응답은 오직 JSON 객체만 포함해야 합니다. 설명, 판단 근거 해설, 마크다운, 주석 등 어떤 추가 텍스트도 절대 포함하지 마세요.** 판단 근거는 각 결정 객체의 "reason" 필드에만 한 문장으로 작성하세요.
여러 코인에 대해 동시에 판단할 수 있습니다. 아무 행동도 필요 없으면 decisions를 빈 배열 []로 반환하세요.

{{
  "decisions": [
    {{
      "action": "buy" 또는 "sell" 또는 "hold",
      "ticker": "KRW-XXX",
      "amount_krw": 숫자 (매수 시),
      "sell_ratio": 0~1 사이 숫자 (매도 시),
      "reason": "한 문장 판단 근거"
    }}
  ]
}}

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
- 1회 매수 금액 상한: {MAX_INVEST_KRW:,.0f}원 (이 금액을 초과하는 amount_krw는 자동으로 잘림)
- 매도 가능 여부: 포트폴리오 항목의 sellable=false인 코인은 보유량이 최소주문금액({MIN_ORDER_KRW:,}원) 미달이므로 절대 sell 결정을 내리지 마세요 (hold만 가능)
- 포지션 한도: sellable=true 코인을 최대 {MAX_POSITIONS}개까지 보유 가능. 한도 도달 시 신규 매수 전 기존 부진 코인을 먼저 매도해야 함
- KRW 최저 잔고: 매수 후에도 KRW 잔고가 {MIN_KRW_RESERVE:,}원 이상 남아야 함
- 포지션 정리 우선순위: 손실 깊은 코인 > 모멘텀 약한 코인 > 익절 수익 코인 순으로 매도 고려
- 트레일링 스톱: 보유 코인의 drawdown_from_peak_pct가 {TRAILING_STOP_PCT}% 이하면 (peak 대비 {abs(TRAILING_STOP_PCT)}% 이상 하락) 적극 매도 고려 — 수익을 토해내지 마세요
- 매매 쿨다운: 최근 {TRADE_COOLDOWN_HOURS}시간 내 거래한 코인은 다시 매매 금지 (왕복 거래 방지)

참고: 각 코인의 orderbook 항목에서 buy_pressure > 1이면 매수 우세, < 1이면 매도 우세입니다.

입력으로 시장 심리, 포트폴리오, 기술적 지표, 최근 거래 이력, 과거 판단 정확도가 주어집니다.
이를 종합하여 최적의 매매 결정을 JSON 객체({{"decisions": [...]}})로만 응답하세요.
다시 강조: 응답에는 JSON 객체 외 어떤 텍스트도 포함하지 마세요. 해설, 설명, 판단 과정 모두 금지."""


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

    sellable_count = sum(1 for t, info in portfolio.items()
                         if t != "KRW" and isinstance(info, dict) and info.get("sellable"))
    krw_balance = portfolio.get("KRW", 0)
    status_lines = [
        f"보유 포지션: {sellable_count}/{MAX_POSITIONS}"
        + (" (한도 도달 — 신규 매수 전 기존 코인 정리 필요)" if sellable_count >= MAX_POSITIONS else ""),
        f"KRW 잔고: {krw_balance:,.0f}원"
        + (f" (최저 잔고 {MIN_KRW_RESERVE:,}원 미달 — 신규 매수 전 정리 필요)" if krw_balance < MIN_KRW_RESERVE else ""),
    ]
    status_text = "\n".join(status_lines)

    user_prompt = f"""현재 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}
시장 심리: {market_summary}

상태:
{status_text}

포트폴리오:
{json.dumps(portfolio, ensure_ascii=False, separators=(',', ':'))}

기술적 지표 (상위 거래량 코인):
{json.dumps(indicators_list, ensure_ascii=False, separators=(',', ':'))}

최근 거래 이력 (참고용):
{json.dumps(recent_history, ensure_ascii=False, separators=(',', ':')) if recent_history else "없음"}

과거 판단 정확도 (판단 시점 가격 → 현재 가격, ✓=정확 ✗=오판 ≈=노이즈; {EVAL_MIN_HOURS}h+ 경과한 거래만 평가):
{evaluate_past_decisions()}"""

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["claude", "-p", user_prompt, "--output-format", "json", "--model", CLAUDE_MODEL,
         "--tools", "", "--effort", "low",
         "--system-prompt", _SYSTEM_PROMPT,
         "--exclude-dynamic-system-prompt-sections"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"claude CLI 오류 (code={result.returncode}): {error_msg}")

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error(f"Claude CLI 응답 파싱 실패: {result.stdout[:300]}")
        return []

    # 토큰 사용량 추적
    usage = response.get("usage", {})
    input_tok = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    cost = response.get("total_cost_usd", 0)
    num_turns = response.get("num_turns", 1)
    _save_token_usage(input_tok, output_tok, cost)
    log.info(f"[토큰] 입력 {input_tok:,} / 출력 {output_tok:,} / ${cost:.4f} / turns={num_turns}")

    # 출력 토큰이 비정상적으로 큰데 result가 짧으면 디버그 덤프
    raw_result = response.get("result", "")

    raw = response.get("result", "").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    # JSON 시작 위치 찾기 ('[' 또는 '{')
    json_start = -1
    for i, ch in enumerate(raw):
        if ch in "[{":
            json_start = i
            break
    if json_start == -1:
        log.error(f"AI 응답에 JSON 없음: {raw[:300]}")
        return []
    try:
        # raw_decode는 첫 JSON만 파싱하고 뒤 텍스트는 무시
        parsed, _ = json.JSONDecoder().raw_decode(raw[json_start:])
    except json.JSONDecodeError:
        log.error(f"AI 응답 JSON 파싱 실패: {raw[:300]}")
        return []
    # 스키마: {"decisions": [...]} 형태에서 배열 추출
    if isinstance(parsed, dict):
        if "decisions" in parsed:
            return parsed["decisions"]
        # 단일 dict (하위 호환)
        return [parsed]
    return parsed


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
    coin_balances = []
    for b in balances:
        currency = b["currency"]
        amount = float(b["balance"])
        if amount > 0:
            if currency == "KRW":
                portfolio["KRW"] = round(amount, 0)
            else:
                coin_balances.append((currency, amount, float(b.get("avg_buy_price", 0))))

    # 보유 코인 시세 일괄 조회 (KRW 마켓에 존재하는 것만)
    prices = {}
    if coin_balances:
        try:
            krw_markets = set(pyupbit.get_tickers(fiat="KRW"))
        except Exception:
            krw_markets = set()
        coin_tickers = [f"KRW-{c}" for c, _, _ in coin_balances if f"KRW-{c}" in krw_markets]
        if coin_tickers:
            try:
                result = pyupbit.get_current_price(coin_tickers)
                if isinstance(result, dict):
                    prices = result
                elif isinstance(result, (int, float)) and len(coin_tickers) == 1:
                    prices = {coin_tickers[0]: result}
            except Exception as e:
                log.warning(f"보유 코인 시세 일괄 조회 실패: {e}")

    for currency, amount, avg_price in coin_balances:
        ticker = f"KRW-{currency}"
        current_price = prices.get(ticker, 0)
        if not current_price:
            continue
        value_krw = round(amount * current_price, 0)
        portfolio[ticker] = {
            "amount": amount,
            "avg_buy_price": avg_price,
            "current_price": current_price,
            "profit_pct": round((current_price / avg_price - 1) * 100, 2) if avg_price > 0 else 0,
            "value_krw": value_krw,
            "sellable": value_krw >= MIN_ORDER_KRW,
        }
    return portfolio


# ──────────────────────────────────────────────
# AI 판단 검증
# ──────────────────────────────────────────────
def validate_decisions(decisions: list[dict], portfolio: dict, valid_tickers: set) -> list[dict]:
    """AI 응답을 검증하여 유효한 결정만 반환"""
    validated = []
    seen_tickers = set()
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

        # 같은 코인 중복 판단 방지
        if action in ("buy", "sell") and ticker in seen_tickers:
            log.warning(f"[검증 실패] {ticker} 중복 판단 무시")
            continue

        # 티커 검증
        if ticker not in valid_tickers:
            log.warning(f"[검증 실패] 존재하지 않는 티커: {ticker}")
            continue

        # 매도 검증: 보유 여부
        if action == "sell" and ticker not in portfolio:
            log.warning(f"[검증 실패] {ticker} 미보유 상태에서 매도 시도")
            continue

        # 매도 검증: 최소주문금액 미달
        if action == "sell" and not portfolio.get(ticker, {}).get("sellable", False):
            log.warning(f"[검증 실패] {ticker} 보유량이 최소주문금액 미달, 매도 불가")
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
        if action in ("buy", "sell"):
            seen_tickers.add(ticker)

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
def _place_order_with_retry(order_func, *args, max_retries: int = 5) -> dict | None:
    """주문 실행. 네트워크 에러 등으로 실패 시 지수 백오프 재시도. 성공한 dict 응답 반환, 끝까지 실패 시 None."""
    backoff = [2, 5, 10, 20, 30]  # 누적 67초까지 대기
    for attempt in range(max_retries):
        try:
            result = order_func(*args)
            if isinstance(result, dict) and "uuid" in result:
                return result
            log.warning(f"[주문 재시도 {attempt+1}/{max_retries}] 응답 이상: {result}")
        except Exception as e:
            log.warning(f"[주문 재시도 {attempt+1}/{max_retries}] 예외: {e}")
        if attempt < max_retries - 1:
            wait_sec = backoff[attempt] if attempt < len(backoff) else backoff[-1]
            if _shutdown_event.wait(wait_sec):
                return None
    return None


def execute_trade(upbit, decision: dict, portfolio: dict, bypass_cooldown: bool = False) -> dict | None:
    """매매 실행. 체결 시 {action, ticker, fill_price, fill_qty, fill_krw, fee} 반환, 취소/실패 시 None."""
    action = decision.get("action", "hold")
    ticker = decision.get("ticker", "")
    reason = decision.get("reason", "")
    reason_md = _md_escape(reason)

    if action == "buy":
        if not bypass_cooldown and _is_in_cooldown(ticker):
            log.warning(f"[BUY 취소] {ticker} 최근 {TRADE_COOLDOWN_HOURS}시간 내 거래 — 쿨다운 차단")
            return None
        krw_balance = portfolio.get("KRW", 0)
        amount_krw = float(decision.get("amount_krw", 0))
        max_invest = krw_balance * INVEST_RATIO
        amount_krw = min(amount_krw, max_invest, MAX_INVEST_KRW)

        if not check_concentration(ticker, amount_krw, portfolio):
            return None

        # 보유 포지션 수 한도 (신규 진입만 차단, 기존 코인 추가 매수는 허용)
        sellable_count = sum(1 for t, info in portfolio.items()
                             if t != "KRW" and isinstance(info, dict) and info.get("sellable"))
        if ticker not in portfolio and sellable_count >= MAX_POSITIONS:
            log.warning(f"[BUY 취소] 보유 포지션 한도 초과 ({sellable_count}/{MAX_POSITIONS})")
            return None

        # KRW 최저 잔고 보호
        if krw_balance - amount_krw < MIN_KRW_RESERVE:
            log.warning(f"[BUY 취소] 매수 후 KRW 잔고 {krw_balance - amount_krw:,.0f}원이 최저 잔고 {MIN_KRW_RESERVE:,}원 미달")
            return None

        if amount_krw < MIN_ORDER_KRW:
            log.warning(f"[BUY 취소] 주문금액 {amount_krw:,.0f}원이 최소금액 미달")
            return None

        # 분할 매수
        split_count = SPLIT_ORDER_COUNT if amount_krw >= MIN_SPLIT_KRW else 1
        split_amount = round(amount_krw / split_count, 0)
        log.info(f"[BUY] {ticker} 총 {amount_krw:,.0f}원 ({split_count}회 분할) | 이유: {reason}")

        total_filled_qty = 0.0
        total_filled_krw = 0.0
        total_fee = 0.0
        for i in range(split_count):
            result = _place_order_with_retry(upbit.buy_market_order, ticker, split_amount)
            log.info(f"[BUY {i+1}/{split_count}] {result}")
            if result:
                time.sleep(1)
                order = upbit.get_order(result["uuid"])
                if order and "trades" in order:
                    for t in order["trades"]:
                        qty = float(t["volume"])
                        px = float(t["price"])
                        total_filled_qty += qty
                        total_filled_krw += px * qty
                    total_fee += float(order.get("paid_fee", 0))
            else:
                log.warning(f"[BUY {i+1}/{split_count} 실패] 재시도 후에도 실패")
                break
            if i < split_count - 1:
                time.sleep(SPLIT_ORDER_DELAY)

        if total_filled_qty > 0:
            avg_price = round(total_filled_krw / total_filled_qty, 0)
            log.info(f"[BUY 완료] {ticker} {total_filled_qty:.6f}개 @ 평균 {avg_price:,.0f}원 | 체결금액: {total_filled_krw:,.0f}원 | 수수료: {total_fee:,.0f}원")
            send_telegram(f"🟢 *매수 체결* | {ticker}\n{total_filled_qty:.6f}개 @ {avg_price:,.0f}원 ({split_count}회 분할)\n체결금액: {total_filled_krw:,.0f}원 | 수수료: {total_fee:,.0f}원\n사유: {reason_md}")
            return {
                "action": "buy", "ticker": ticker,
                "fill_price": avg_price, "fill_qty": total_filled_qty,
                "fill_krw": total_filled_krw, "fee": total_fee,
            }
        else:
            send_telegram(f"⚠️ *매수 실패* | {ticker}\n{amount_krw:,.0f}원")
            return None

    elif action == "sell":
        if not bypass_cooldown and _is_in_cooldown(ticker):
            log.warning(f"[SELL 취소] {ticker} 최근 {TRADE_COOLDOWN_HOURS}시간 내 거래 — 쿨다운 차단")
            return None
        sell_ratio = float(decision.get("sell_ratio", 1.0))
        coin_info = portfolio.get(ticker)
        if not coin_info:
            log.warning(f"[SELL 취소] {ticker} 보유량 없음")
            return None

        sell_amount = coin_info["amount"] * sell_ratio
        if sell_amount * coin_info["current_price"] < MIN_ORDER_KRW:
            log.warning(f"[SELL 취소] 매도 금액이 최소금액 미달")
            return None

        pnl = coin_info["profit_pct"]
        sell_krw_est = sell_amount * coin_info["current_price"]
        split_count = SPLIT_ORDER_COUNT if sell_krw_est >= MIN_SPLIT_KRW else 1
        split_qty = sell_amount / split_count
        log.info(f"[SELL] {ticker} {sell_ratio*100:.0f}% ({sell_amount:.6f}개, {split_count}회 분할) | 이유: {reason}")

        total_filled_qty = 0.0
        total_filled_krw = 0.0
        total_fee = 0.0
        for i in range(split_count):
            result = _place_order_with_retry(upbit.sell_market_order, ticker, split_qty)
            log.info(f"[SELL {i+1}/{split_count}] {result}")
            if result:
                time.sleep(1)
                order = upbit.get_order(result["uuid"])
                if order and "trades" in order:
                    for t in order["trades"]:
                        qty = float(t["volume"])
                        px = float(t["price"])
                        total_filled_qty += qty
                        total_filled_krw += px * qty
                    total_fee += float(order.get("paid_fee", 0))
            else:
                log.warning(f"[SELL {i+1}/{split_count} 실패] 재시도 후에도 실패")
                break
            if i < split_count - 1:
                time.sleep(SPLIT_ORDER_DELAY)

        if total_filled_qty > 0:
            avg_price = round(total_filled_krw / total_filled_qty, 0)
            log.info(f"[SELL 완료] {ticker} {total_filled_krw:,.0f}원 @ 평균 {avg_price:,.0f}원 | 손익: {pnl:+.1f}% | 수수료: {total_fee:,.0f}원")
            send_telegram(f"🔴 *매도 체결* | {ticker}\n금액: {total_filled_krw:,.0f}원 @ {avg_price:,.0f}원 ({split_count}회 분할)\n손익: {pnl:+.1f}% | 수수료: {total_fee:,.0f}원\n사유: {reason_md}")
            return {
                "action": "sell", "ticker": ticker,
                "fill_price": avg_price, "fill_qty": total_filled_qty,
                "fill_krw": total_filled_krw, "fee": total_fee,
            }
        else:
            send_telegram(f"⚠️ *매도 실패* | {ticker}")
            return None

    return None


# ──────────────────────────────────────────────
# 안전 가드: 손절 / 익절 / 트레일링 스톱
# ──────────────────────────────────────────────
def enforce_safety_exits(upbit, portfolio: dict) -> list[dict]:
    """STOP_LOSS / TAKE_PROFIT / TRAILING_STOP 위반 코인 강제 매도.
    AI 호출 전 방어선. 체결된 안전 매도 결정 목록 반환 (호출자는 portfolio 재조회 필요)."""
    executed = []
    for ticker in list(portfolio.keys()):
        if ticker == "KRW":
            continue
        info = portfolio.get(ticker)
        if not isinstance(info, dict) or not info.get("sellable"):
            continue
        profit_pct = info.get("profit_pct", 0)
        drawdown = info.get("drawdown_from_peak_pct", 0)
        peak_price = info.get("peak_price", 0)
        avg_price = info.get("avg_buy_price", 0)

        decision = None
        if profit_pct <= STOP_LOSS_PCT:
            decision = {"action": "sell", "ticker": ticker, "sell_ratio": 1.0,
                        "reason": f"손절 가드 ({profit_pct:+.1f}%)"}
        elif profit_pct >= TAKE_PROFIT_PCT:
            decision = {"action": "sell", "ticker": ticker, "sell_ratio": 0.5,
                        "reason": f"익절 가드 ({profit_pct:+.1f}%, 50% 매도)"}
        elif drawdown <= TRAILING_STOP_PCT and peak_price > avg_price > 0:
            decision = {"action": "sell", "ticker": ticker, "sell_ratio": 1.0,
                        "reason": f"트레일링 가드 (peak 대비 {drawdown:+.1f}%)"}

        if not decision:
            continue
        log.info(f"[안전 가드 발동] {ticker}: {decision['reason']}")
        fill = execute_trade(upbit, decision, portfolio, bypass_cooldown=True)
        if fill:
            save_trade_record({
                "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                "trade_price": fill["fill_price"],
                **decision,
            })
            executed.append(decision)
    return executed


# ──────────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────────
_shutdown_requested = False
_shutdown_event = threading.Event()
_last_error_alert_ts = 0.0


def _signal_handler(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
    _shutdown_event.set()
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
            _maybe_send_daily_report(upbit)

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
            top_tickers = [v[0] for v in volumes if v[0] not in STABLECOINS][:MAX_COINS_TO_ANALYZE]

            # 2. 포트폴리오 조회 + peak 가격 업데이트
            time.sleep(1)
            portfolio = get_portfolio(upbit)
            annotate_peaks(portfolio)
            log.info(f"현재 포트폴리오: {portfolio}")

            # 2-1. 안전 가드 (손절/익절/트레일링) — AI보다 먼저 강제 정리
            safety_executed = enforce_safety_exits(upbit, portfolio)
            if safety_executed:
                log.info(f"안전 가드로 {len(safety_executed)}건 매도, 포트폴리오 재조회")
                time.sleep(1)
                portfolio = get_portfolio(upbit)
                annotate_peaks(portfolio)

            # 매도 가능한 보유 코인만 분석 대상에 포함 (dust 제외)
            holding_tickers = [
                t for t, info in portfolio.items()
                if t != "KRW" and isinstance(info, dict) and info.get("sellable", False) and t not in top_tickers
            ]
            analyze_tickers = top_tickers + holding_tickers
            log.info(f"분석 대상 코인: {analyze_tickers}")

            # 3. 기술적 지표 + 오더북 수집
            indicators_list = []
            for ticker in analyze_tickers:
                ind = get_technical_indicators(ticker)
                if ind:
                    ob = get_orderbook_summary(ticker)
                    if ob:
                        ind["orderbook"] = ob
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

            # 7. 매매 실행 + 판단 요약 수집
            summary_lines = [f"🛡 {d['ticker']}: {_md_escape(d['reason'])}" for d in safety_executed]
            for decision in decisions:
                action = decision.get("action", "hold")
                ticker = decision.get("ticker", "")
                reason = decision.get("reason", "")
                reason_md = _md_escape(reason)

                if action == "hold":
                    log.info(f"[HOLD] {ticker} | {reason}")
                    summary_lines.append(f"⏸ {ticker}: {reason_md}")
                    continue

                fill = execute_trade(upbit, decision, portfolio)
                if fill:
                    summary_lines.append(f"{'🟢 BUY' if action == 'buy' else '🔴 SELL'} {ticker}: {reason_md}")
                    save_trade_record({
                        "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "trade_price": fill["fill_price"],
                        **decision,
                    })
                    # 후속 매매를 위해 portfolio 재조회 (KRW/포지션 한도 stale 방지)
                    time.sleep(1)
                    portfolio = get_portfolio(upbit)
                    annotate_peaks(portfolio)
                else:
                    summary_lines.append(f"⏭ {ticker} 취소: {reason_md}")

            # 사이클 판단 요약 + 총 자산 텔레그램 전송
            now = datetime.now().strftime('%H:%M')
            total_value = portfolio.get("KRW", 0)
            for t, info in portfolio.items():
                if t != "KRW" and isinstance(info, dict):
                    total_value += info.get("value_krw", 0)
            asset_line = f"💰 총 자산: {total_value:,.0f}원 (현금 {portfolio.get('KRW', 0):,.0f}원)"
            if summary_lines:
                summary = f"📋 *사이클 요약* ({now})\n" + "\n".join(summary_lines) + f"\n{asset_line}"
            else:
                summary = f"📋 *사이클 요약* ({now})\n판단: 홀드\n{asset_line}"
            send_telegram(summary)

        except Exception as e:
            log.error(f"사이클 오류: {e}", exc_info=True)
            global _last_error_alert_ts
            if time.time() - _last_error_alert_ts > ERROR_ALERT_INTERVAL:
                send_telegram(f"⚠️ *사이클 오류*\n{type(e).__name__}: {_md_escape(str(e)[:200])}")
                _last_error_alert_ts = time.time()
            log.info(f"오류 발생, {RETRY_INTERVAL_SECONDS}초 후 재시도...")
            if _shutdown_event.wait(RETRY_INTERVAL_SECONDS):
                break
            continue

        log.info(f"다음 분석까지 {TRADE_INTERVAL_SECONDS//60}분 대기 (급변동 감지 중)...\n")
        if not wait_with_alert(upbit, portfolio):
            log.info(f"급변동 감지! {ALERT_COOLDOWN_SECONDS}초 쿨다운 후 사이클 재실행")
            if _shutdown_event.wait(ALERT_COOLDOWN_SECONDS):
                break

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

    # 기준 가격 저장 (일괄 조회)
    base_prices = {}
    if watch_tickers:
        try:
            result = pyupbit.get_current_price(watch_tickers)
            if isinstance(result, dict):
                base_prices = {t: p for t, p in result.items() if p}
            elif isinstance(result, (int, float)) and len(watch_tickers) == 1:
                base_prices = {watch_tickers[0]: result}
        except Exception:
            pass

    if not base_prices:
        if _shutdown_event.wait(TRADE_INTERVAL_SECONDS):
            return True
        return True

    elapsed = 0
    check_interval = 60  # 1분마다 체크

    while elapsed < TRADE_INTERVAL_SECONDS:
        if _shutdown_event.wait(check_interval):
            return True
        elapsed += check_interval

        # 급변동 체크 (일괄 조회)
        try:
            current_prices = pyupbit.get_current_price(list(base_prices.keys()))
            if isinstance(current_prices, (int, float)) and len(base_prices) == 1:
                current_prices = {list(base_prices.keys())[0]: current_prices}
            if isinstance(current_prices, dict):
                for t, base_price in base_prices.items():
                    current = current_prices.get(t)
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

        # 일일 성과 리포트 (날짜 바뀌면 1회)
        _maybe_send_daily_report(upbit)

    return True


def _maybe_send_daily_report(upbit):
    """오늘 아직 리포트를 안 보냈으면 발송. 마지막 발송 날짜를 파일에 영속화."""
    today = datetime.now().strftime('%Y-%m-%d')
    last_sent = ""
    try:
        with open(LAST_REPORT_FILE, "r") as f:
            last_sent = f.read().strip()
    except FileNotFoundError:
        pass
    if last_sent == today:
        return
    if send_daily_report(upbit):
        try:
            with open(LAST_REPORT_FILE, "w") as f:
                f.write(today)
        except Exception as e:
            log.warning(f"리포트 발송일 저장 실패: {e}")


def send_daily_report(upbit) -> bool:
    """일일 거래 요약 + 총 자산 현황을 텔레그램으로 전송. 성공 시 True."""
    try:
        portfolio = get_portfolio(upbit)
        total_value = portfolio.get("KRW", 0)
        holdings_text = []
        for t, info in portfolio.items():
            if t == "KRW":
                continue
            total_value += info["value_krw"]
            holdings_text.append(f"  {t}: {info['value_krw']:,.0f}원 ({info['profit_pct']:+.1f}%)")

        # 전일 거래 이력 (자정 리포트는 직전 날짜 기준)
        history = load_trade_history()
        report_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        day_trades = [h for h in history if h.get("time", "").startswith(report_date)]

        report = f"📊 *일일 리포트* ({report_date})\n"
        report += f"총 자산: {total_value:,.0f}원\n"
        report += f"현금: {portfolio.get('KRW', 0):,.0f}원\n"
        if holdings_text:
            report += "보유:\n" + "\n".join(holdings_text) + "\n"
        report += f"거래: {len(day_trades)}건"

        # 토큰 사용량
        token_usage = _load_token_usage()
        day_usage = token_usage.get(report_date, {})
        if day_usage:
            report += f"\nAI 비용: ${day_usage['cost_usd']:.2f} ({day_usage['calls']}회 호출, 입력 {day_usage['input_tokens']:,} / 출력 {day_usage['output_tokens']:,} 토큰)"

        log.info(report)
        send_telegram(report)
        return True
    except Exception as e:
        log.warning(f"일일 리포트 생성 실패: {e}")
        return False


if __name__ == "__main__":
    main()