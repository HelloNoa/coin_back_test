# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

업비트 AI 자동 암호화폐 매매 봇. Claude Code CLI(`claude -p`)를 subprocess로 호출하여 기술적 지표, 시장 심리, 포트폴리오 상태를 기반으로 매수/매도/홀드 결정을 내린다. 업비트 거래소의 KRW 마켓을 대상으로 한다.

## 실행 방법

```bash
# 가상환경 활성화
source .venv/bin/activate

# 의존성 설치
pip install pyupbit pandas requests python-dotenv

# 트레이딩 봇 실행 (.env에 API 키 필요)
python upbit_ai_trader.py
```

## 환경변수 (.env)

- `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` — 업비트 API 인증 키
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — 텔레그램 알림 (선택)
- `ANTHROPIC_API_KEY` — Claude CLI subprocess 환경에서 의도적으로 제외됨

## 아키텍처

**`upbit_ai_trader.py`** — 실전 매매 봇, 30분 주기 무한 루프:
1. 업비트 전체 시세 API 1회 호출로 거래량 상위 코인 수집 + 보유 코인도 분석 대상에 포함
2. 다중 타임프레임 기술적 지표 계산 (15분/1시간/일봉): RSI, MACD, 볼린저밴드, 거래량 비율
3. 오더북(호가) 분석: 매수/매도 압력 비율, 스프레드
4. 시장 심리 수집 (공포/탐욕 지수, 바이낸스 비교를 통한 김치 프리미엄)
5. Claude Code CLI subprocess 호출 → JSON 배열 형태의 매매 결정 수신
6. AI 응답 검증 (잘못된 티커, 미보유 매도, 범위 초과 등 필터링)
7. 분할 매수/매도 (3회 분할, 5초 간격), 포지션 집중도 제한(단일 코인 최대 30%), 손절(-15%), 익절(+30%)
8. 주문 체결 확인 (체결가, 수량, 수수료 조회)
9. 과거 AI 판단 정확도를 평가하여 다음 사이클에 피드백
10. 대기 중 1분마다 보유코인+BTC 급변동(±5%) 감지 → 즉시 사이클 재실행
11. `trading_log.txt`에 로그 기록 (5MB × 5파일 로테이션) 및 텔레그램 알림
12. 자정 일일 성과 리포트 텔레그램 전송
13. SIGINT/SIGTERM 수신 시 현재 사이클 완료 후 안전 종료

**`upbit_ai_dryrun.py`** — 드라이런 봇 (전략 검증용):
- `upbit_ai_trader.py`의 분석/AI 함수를 임포트하여 동일한 로직 사용
- 실제 시세 데이터 사용, 주문은 가상 처리 (수수료 0.05% 반영)
- 가상 포트폴리오 `dryrun_portfolio.json`에 저장 (재시작 유지)
- AI 응답 검증, 집중도 체크 등 실전과 동일한 안전장치 적용

**`ignore/`** — 실험/스크래치 스크립트 (git 제외)

## 주요 설계 결정

- AI 판단은 API 직접 호출이 아닌 `claude -p` CLI 셸아웃 방식 → 호스트에 Claude Code CLI 설치 및 인증 필수
- subprocess 환경에서 `ANTHROPIC_API_KEY`를 의도적으로 제거하여 충돌 방지
- 거래 이력은 `trade_history.json`에 최근 50건만 유지, 마지막 10건을 Claude에 컨텍스트로 전달
- 거래량 수집은 업비트 전체 시세 API(`/v1/ticker`) 1회 호출 (실패 시 개별 조회 폴백)
- 분할 주문(3회)으로 슬리피지 최소화, 주문 후 체결 내역 확인
- 업비트 API 쓰로틀링 방지를 위해 요청 간 `time.sleep()` 적용

## Git 커밋 규칙

- 커밋 메시지에 `Co-Authored-By: Claude` 또는 Claude Code 관련 링크를 포함하지 말 것
- 간결하고 명확한 커밋 메시지 작성 (conventional commits 형식)