# Tradenance

Trade.xyz vs Binance 주식/ETF/원자재 퍼프 펀딩레이트 아비트라지 모니터 + 페이퍼 트레이더

## 개요

Trade.xyz (Hyperliquid HIP-3)는 **0.5x 펀딩 승수**를 적용하여 Binance TRADIFI_PERPETUAL 대비 구조적 펀딩레이트 차이가 발생. 이 도구는 교차 상장 26개 종목의 펀딩레이트 스프레드를 실시간 추적하고, 실비용 기반 페이퍼 트레이딩으로 전략을 검증.

## 모니터링 대상 (26개)

**주식 (17)**: AAPL, AMZN, BABA, COIN, CRCL, GOOGL, HOOD, INTC, META, MSFT, MSTR, MU, NVDA, PLTR, SNDK, TSLA, TSM

**ETF (2)**: EWJ, EWY

**원자재 (7)**: CL(WTI), COPPER, NATGAS, GOLD, SILVER, PLATINUM, BRENTOIL

## 파일 구조

```
funding_monitor.py   # 펀딩레이트 스캔 + 히스토리 분석
paper_trader.py      # 실비용 페이퍼 트레이딩 시뮬레이터
requirements.txt     # 의존성
data/                # 스냅샷, 거래 로그, PnL 기록 (gitignored)
```

## 사용법

```bash
# 설치
pip install -r requirements.txt

# === 모니터링 ===
python funding_monitor.py                    # 1회 스캔
python funding_monitor.py --loop 30          # 30분마다 반복
python funding_monitor.py --history 14 --top 5  # 14일 히스토리 TOP5

# === 페이퍼 트레이딩 ===
python paper_trader.py                       # 기본 ($10K, 3포지션, 8시간 주기)
python paper_trader.py --capital 5000        # 자본 $5K
python paper_trader.py --max-pos 5           # 최대 5포지션
python paper_trader.py --interval 60         # 60분 주기
python paper_trader.py --min-spread 0.05     # 최소 스프레드 0.05%/8h
python paper_trader.py --min-oi 2            # 최소 OI $2M
python paper_trader.py --reset               # 상태 초기화

# Termux 백그라운드
nohup python -u paper_trader.py --capital 10000 --interval 480 > paper.log 2>&1 &
```

## 비용 구조

| 항목 | Trade.xyz | Binance |
|------|-----------|---------|
| Taker fee | 0.009% (Growth Mode) | 0.05% |
| Maker fee | -0.003% (rebate) | 0.02% |
| 슬리피지 | OI 대비 추정 | OI 대비 추정 |

$2,000 포지션 기준 양쪽 진입비용: ~$1.58

## 페이퍼 트레이더 로직

1. 전 종목 펀딩레이트 스캔
2. |스프레드| 큰 순서로 소팅, 유동성 필터
3. 보유 포지션에 펀딩 적용
4. 스프레드 반전/축소 시 청산
5. 새 기회에 진입 (로테이션)
6. PnL 기록 (수수료, 슬리피지, 베이시스 전부 반영)

## 데이터 파일

- `data/funding_snapshot.jsonl` — 스캔별 전 종목 스냅샷
- `data/spread_history.jsonl` — 히스토리 분석 요약
- `data/paper_trades.jsonl` — 페이퍼 거래 로그
- `data/paper_pnl.jsonl` — 기간별 PnL 기록
- `data/paper_state.json` — 현재 포지션/계좌 상태

## 구조적 스프레드 원인

- Trade.xyz: 1시간 정산, **0.5x 펀딩 승수** (주식 캐리 비용 조정)
- Binance: 8시간 정산, 표준 펀딩 공식
- 결과: Trade.xyz가 항상 "덜 음수" → Binance SHORT + Trade.xyz LONG 기본 전략
- 단, 스프레드 방향이 시간대별로 뒤집힐 수 있음 → 로테이션 필수
