# 업비트 AI 자동매매 봇

Claude Code CLI를 활용한 암호화폐 자동매매 봇. 기술적 지표, 시장 심리, 김치 프리미엄을 분석하여 AI가 매매를 판단합니다.

## 사전 준비

1. Python 3.12+
2. Claude Code CLI 설치 및 로그인 (`claude` 명령어 사용 가능 상태)
3. 업비트 API 키 발급

## 설치

```bash
pip install pyupbit pandas requests python-dotenv
```

## 환경변수 설정

`.env` 파일을 프로젝트 루트에 생성:

```env
UPBIT_ACCESS_KEY=발급받은키
UPBIT_SECRET_KEY=발급받은키

# 텔레그램 알림 (선택)
TELEGRAM_BOT_TOKEN=봇토큰
TELEGRAM_CHAT_ID=채팅ID
```

## 스크립트

### 실전 매매 — `upbit_ai_trader.py`

실제 업비트 계좌로 자동매매를 실행합니다.

```bash
python upbit_ai_trader.py
```

- 30분마다 분석 사이클 실행 (대기 중 급변동 감지 시 즉시 재실행)
- KRW 마켓 거래대금 상위 10개 + 보유 코인 분석
- 다중 타임프레임 지표 (15분/1시간/일봉) + 오더북 매수/매도 압력
- 분할 매수/매도 (3회 분할, 슬리피지 최소화)
- 주문 체결 확인 (실제 체결가, 수량, 수수료 조회)
- AI 응답 검증 (잘못된 티커, 미보유 매도 등 사전 필터링)
- 단일 코인 최대 비중 30% 제한
- 과거 AI 판단 정확도 추적 및 피드백
- 매매 시 텔레그램 알림 + 자정 일일 성과 리포트
- Ctrl+C 시 현재 사이클 완료 후 안전 종료
- 거래 이력: `trade_history.json` / 로그: `trading_log.txt` (5MB × 5파일 로테이션)

**주요 설정값** (`upbit_ai_trader.py` 상단):

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `TRADE_INTERVAL_SECONDS` | 1800 | 분석 주기 (초) |
| `MAX_COINS_TO_ANALYZE` | 10 | 분석 코인 수 |
| `INVEST_RATIO` | 0.3 | 1회 최대 투자 비율 |
| `MAX_CONCENTRATION` | 0.3 | 단일 코인 최대 비중 |
| `MIN_ORDER_KRW` | 6000 | 최소 주문 금액 |
| `STOP_LOSS_PCT` | -15 | 손절 기준 (%) |
| `TAKE_PROFIT_PCT` | 30 | 익절 기준 (%) |
| `PRICE_ALERT_PCT` | 5 | 급변동 감지 기준 (%) |
| `SPLIT_ORDER_COUNT` | 3 | 분할 주문 횟수 |

### 드라이런 — `upbit_ai_dryrun.py`

실제 시세를 사용하되 주문은 가상으로 처리합니다. 전략 검증용.

```bash
python upbit_ai_dryrun.py                    # 기본: 100만원, 10분 간격, 무한 실행
python upbit_ai_dryrun.py --krw 500000       # 초기 자본금 50만원
python upbit_ai_dryrun.py --cycles 5         # 5사이클 실행 후 종료 + 성과 리포트
python upbit_ai_dryrun.py --interval 300     # 5분 간격
python upbit_ai_dryrun.py --reset            # 포트폴리오 초기화 후 시작
```

- 실전과 동일한 분석/AI/검증 로직 사용 (임포트)
- 수수료 0.05% 반영, 집중도 체크 적용
- 매 사이클마다 총 자산/손익 리포트 출력
- 가상 포트폴리오: `dryrun_portfolio.json` (재시작해도 유지)
- 거래 이력: `dryrun_history.json` / 로그: `dryrun_log.txt`

## AI 판단 기준

- RSI 과매도/과매수 (15분/1시간/일봉 다중 확인)
- MACD 모멘텀
- 볼린저밴드 위치
- 거래량 급증/감소 (20기간 평균 대비)
- 오더북 매수/매도 압력 비율
- 일봉 추세 방향 (MA5 vs MA20)
- 공포탐욕지수
- 김치 프리미엄 (업비트 vs 바이낸스)
- 과거 AI 판단 정확도 피드백
- 손절/익절 기준
