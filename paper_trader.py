"""
Tradenance Paper Trader — 실비용 반영 펀딩비 아비트라지 시뮬레이터

실제 거래와 동일한 비용 구조 반영:
  - 진입/청산 수수료 (taker)
  - 슬리피지 추정 (포지션 크기 vs OI 기반, 1.5x 안전계수)
  - 가격 괴리 (basis) — 양쪽 mark price 차이
  - 펀딩비 수취/지불 — XYZ 1h 정산, BN 8h 정산 분리 적용
  - 로테이션 비용 — 종목 교체 시 양쪽 청산+재진입

사용법:
  python paper_trader.py                         # 기본 ($10K, 최대 3포지션, 1시간 주기)
  python paper_trader.py --capital 5000          # 자본 $5K
  python paper_trader.py --max-pos 5             # 최대 5포지션
  python paper_trader.py --min-spread 0.05       # 최소 스프레드 0.05%/8h
  python paper_trader.py --min-oi 2              # 최소 OI $2M
  python paper_trader.py --reset                 # 상태 초기화
"""

import requests
import time
import json
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from tabulate import tabulate

HL_API = "https://api.hyperliquid.xyz/info"

OVERLAP_PAIRS = {
    "AAPL": "AAPLUSDT", "AMZN": "AMZNUSDT", "BABA": "BABAUSDT",
    "COIN": "COINUSDT", "CRCL": "CRCLUSDT", "GOOGL": "GOOGLUSDT",
    "HOOD": "HOODUSDT", "INTC": "INTCUSDT", "META": "METAUSDT",
    "MSFT": "MSFTUSDT", "MSTR": "MSTRUSDT", "MU": "MUUSDT",
    "NVDA": "NVDAUSDT", "PLTR": "PLTRUSDT", "SNDK": "SNDKUSDT",
    "TSLA": "TSLAUSDT", "TSM": "TSMUSDT",
    "EWJ": "EWJUSDT", "EWY": "EWYUSDT",
    "CL": "CLUSDT", "COPPER": "COPPERUSDT", "NATGAS": "NATGASUSDT",
    "GOLD": "XAUUSDT", "SILVER": "XAGUSDT", "PLATINUM": "XPTUSDT",
    "BRENTOIL": "BZUSDT",
}

XYZ_TAKER_FEE = 0.00009    # 0.009% Growth Mode
BN_TAKER_FEE = 0.0005      # 0.05%
SLIPPAGE_SAFETY = 1.0       # 슬리피지 추정 (보수적 1.5→현실적 1.0)
LIMIT_ORDER_BA_FACTOR = 0.5 # limit order 사용 가정 → BA 비용 50% 할인
INTERVAL_MIN = 60           # 1시간 주기 (Trade.xyz 정산 주기)
BN_SETTLE_HOURS = {0, 8, 16}  # Binance 정산 시각 (UTC)
MIN_XYZ_VOL_24H = 1_000_000   # XYZ 최소 24h 거래량 $1M
MAX_POS_OI_RATIO = 0.02        # 포지션은 MinOI의 최대 2%
DEFAULT_LEVERAGE = 5            # 기본 5x 레버리지 (양쪽 delta-neutral)
MAX_BA_PCT = 0.0006             # 양쪽 합산 BA > 0.06%면 스킵
MAX_BREAKEVEN_HOURS = 18        # 손익분기 예상 18시간 초과 시 스킵
MIN_HOLD_HOURS = 6              # 최소 보유 시간 (SPREAD_COLLAPSED 조기 종료 방지)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
TRADES_FILE = os.path.join(DATA_DIR, "paper_trades.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "paper_state.json")
PNL_FILE = os.path.join(DATA_DIR, "paper_pnl.jsonl")


def _sf(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def estimate_slippage(notional: float, oi_usd: float) -> float:
    if oi_usd <= 0:
        return 0.005 * SLIPPAGE_SAFETY
    ratio = notional / oi_usd
    if ratio < 0.005:
        base = 0.0001
    elif ratio < 0.01:
        base = 0.0003
    elif ratio < 0.02:
        base = 0.0005
    elif ratio < 0.05:
        base = 0.001
    elif ratio < 0.10:
        base = 0.002
    else:
        base = 0.005
    return base * SLIPPAGE_SAFETY


def is_bn_settlement_hour(utc_hour: int) -> bool:
    return utc_hour in BN_SETTLE_HOURS


@dataclass
class Position:
    ticker: str
    bn_sym: str
    direction: str          # "XYZ_L_BN_S" or "XYZ_S_BN_L"
    size_usd: float         # 노셔널 (마진 × 레버리지)
    xyz_entry_px: float
    bn_entry_px: float
    entry_time: str
    entry_fees: float
    margin_used: float = 0.0
    leverage: int = 5
    funding_pnl: float = 0.0
    xyz_funding_count: int = 0
    bn_funding_count: int = 0
    unrealized_basis: float = 0.0
    entry_ts: float = 0.0          # epoch seconds
    data_fail_streak: int = 0      # 연속 데이터 조회 실패 횟수

    def net_pnl(self) -> float:
        return self.funding_pnl - self.entry_fees + self.unrealized_basis

    def hold_hours(self) -> float:
        return (time.time() - self.entry_ts) / 3600 if self.entry_ts else 0


@dataclass
class PaperAccount:
    initial_capital: float = 10000.0
    capital: float = 10000.0
    positions: dict = field(default_factory=dict)
    total_realized_pnl: float = 0.0
    total_fees_paid: float = 0.0
    total_funding_earned: float = 0.0
    total_trades: int = 0

    def available_capital(self) -> float:
        used = sum(p.margin_used for p in self.positions.values())
        return self.capital - used

    def save(self):
        state = {
            "initial_capital": self.initial_capital,
            "capital": self.capital,
            "total_realized_pnl": self.total_realized_pnl,
            "total_fees_paid": self.total_fees_paid,
            "total_funding_earned": self.total_funding_earned,
            "total_trades": self.total_trades,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    @classmethod
    def load(cls, default_capital: float = 10000.0):
        if not os.path.exists(STATE_FILE):
            return cls(initial_capital=default_capital, capital=default_capital)
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        acct = cls(
            initial_capital=state["initial_capital"],
            capital=state["capital"],
            total_realized_pnl=state["total_realized_pnl"],
            total_fees_paid=state["total_fees_paid"],
            total_funding_earned=state["total_funding_earned"],
            total_trades=state["total_trades"],
        )
        for k, v in state.get("positions", {}).items():
            acct.positions[k] = Position(**v)
        return acct


def fetch_market_data() -> dict:
    # Trade.xyz — 1회 호출로 전종목 (bid/ask 포함)
    resp = requests.post(HL_API, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    xyz = {}
    for meta, ctx in zip(data[0]["universe"], data[1]):
        name = meta["name"].replace("xyz:", "")
        mk = _sf(ctx.get("markPx"))
        mid = _sf(ctx.get("midPx")) or mk
        oi_qty = _sf(ctx.get("openInterest"))
        impacts = ctx.get("impactPxs") or []
        bid = _sf(impacts[0]) if len(impacts) > 0 else mid
        ask = _sf(impacts[1]) if len(impacts) > 1 else mid
        ba_spread = (ask - bid) / mid if mid > 0 else 0
        xyz[name] = {
            "funding_1h": _sf(ctx.get("funding")),
            "mark_px": mk,
            "mid_px": mid,
            "bid": bid,
            "ask": ask,
            "ba_spread": ba_spread,
            "oi_usd": oi_qty * mk if mk else 0,
            "vol_24h": _sf(ctx.get("dayNtlVlm")),
        }

    # Binance — premiumIndex + bookTicker 일괄 조회
    bn = {}
    bn_symbols = set(OVERLAP_PAIRS.values())

    r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10)
    r.raise_for_status()
    for d in r.json():
        sym = d.get("symbol", "")
        if sym in bn_symbols:
            mk = _sf(d.get("markPrice"))
            bn[sym] = {
                "funding_8h": _sf(d.get("lastFundingRate")),
                "mark_px": mk,
                "next_funding": d.get("nextFundingTime", 0),
            }

    r_book = requests.get("https://fapi.binance.com/fapi/v1/ticker/bookTicker", timeout=10)
    if r_book.status_code == 200:
        for d in r_book.json():
            sym = d.get("symbol", "")
            if sym in bn and sym in bn_symbols:
                bid = _sf(d.get("bidPrice"))
                ask = _sf(d.get("askPrice"))
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else bn[sym]["mark_px"]
                bn[sym]["bid"] = bid
                bn[sym]["ask"] = ask
                bn[sym]["ba_spread"] = (ask - bid) / mid if mid > 0 else 0

    for bn_sym in bn_symbols:
        if bn_sym not in bn:
            continue
        try:
            r3 = requests.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": bn_sym}, timeout=10,
            )
            if r3.status_code == 200:
                oi_qty = _sf(r3.json().get("openInterest"))
                bn[bn_sym]["oi_usd"] = oi_qty * bn[bn_sym]["mark_px"]
        except Exception:
            bn[bn_sym]["oi_usd"] = 0
        time.sleep(0.05)

    return {"xyz": xyz, "bn": bn}


def calc_entry_cost(size: float, opp: dict) -> float:
    """진입 시 총 비용: 수수료 + 슬리피지 + 호가스프레드(half, limit 할인 적용)"""
    xyz_slip = estimate_slippage(size, opp["xyz_oi_usd"])
    bn_slip = estimate_slippage(size, opp["bn_oi_usd"])
    xyz_ba_half = opp.get("xyz_ba_spread", 0) / 2 * LIMIT_ORDER_BA_FACTOR
    bn_ba_half = opp.get("bn_ba_spread", 0) / 2 * LIMIT_ORDER_BA_FACTOR
    return size * (XYZ_TAKER_FEE + BN_TAKER_FEE + xyz_slip + bn_slip + xyz_ba_half + bn_ba_half)


def calc_position_size(capital_available: float, pct: float, min_oi_usd: float, leverage: int = DEFAULT_LEVERAGE) -> float:
    """유동성 비례 사이징: 마진 기준 → 노셔널 = 마진 × 레버리지, OI 상한 적용"""
    margin = capital_available * pct
    notional = margin * leverage
    oi_cap = min_oi_usd * MAX_POS_OI_RATIO
    return min(notional, oi_cap, 25000)  # 노셔널 상한 $25K


def scan_opportunities(mkt: dict, min_spread_pct: float, min_oi_m: float) -> list:
    """min_spread_pct: %/8h 단위 (예: 0.04 = 0.04%/8h)"""
    opps = []
    for ticker, bn_sym in OVERLAP_PAIRS.items():
        x = mkt["xyz"].get(ticker)
        b = mkt["bn"].get(bn_sym)
        if not x or not b:
            continue

        xyz_px = x["mark_px"]
        bn_px = b["mark_px"]
        if xyz_px <= 0 or bn_px <= 0:
            continue

        # 거래량 필터
        if x["vol_24h"] < MIN_XYZ_VOL_24H:
            continue

        x8 = x["funding_1h"] * 8
        b8 = b["funding_8h"]
        spread = x8 - b8

        x_oi = x["oi_usd"]
        b_oi = b.get("oi_usd", 0)
        min_oi = min(x_oi, b_oi)

        if abs(spread) < min_spread_pct / 100:
            continue
        if min_oi < min_oi_m * 1e6:
            continue

        xyz_ba = x.get("ba_spread", 0)
        bn_ba = b.get("ba_spread", 0)
        total_ba = xyz_ba + bn_ba

        # BA 스프레드 절대 상한 (양쪽 합산)
        if total_ba > MAX_BA_PCT:
            continue

        # BA > 펀딩 스프레드면 수수료만 내는 거래
        if total_ba > abs(spread):
            continue

        # 손익분기 시간 추정: 왕복 비용 / 시간당 펀딩 수입
        hourly_funding = abs(spread) / 8 * 2000  # $2K 기준 시간당 수입
        round_trip_cost = calc_entry_cost(2000, {
            "xyz_oi_usd": x_oi, "bn_oi_usd": b_oi,
            "xyz_ba_spread": xyz_ba, "bn_ba_spread": bn_ba,
        }) * 2  # 진입 + 청산
        breakeven_h = round_trip_cost / hourly_funding if hourly_funding > 0 else 999
        if breakeven_h > MAX_BREAKEVEN_HOURS:
            continue

        opps.append({
            "ticker": ticker,
            "bn_sym": bn_sym,
            "spread_8h": spread,
            "abs_spread": abs(spread),
            "annual": spread * 3 * 365 * 100,
            "direction": "XYZ_S_BN_L" if spread > 0 else "XYZ_L_BN_S",
            "xyz_px": xyz_px,
            "bn_px": bn_px,
            "xyz_funding_1h": x["funding_1h"],
            "bn_funding_8h": b8,
            "xyz_oi_usd": x_oi,
            "bn_oi_usd": b_oi,
            "min_oi_usd": min_oi,
            "xyz_ba_spread": xyz_ba,
            "bn_ba_spread": bn_ba,
            "total_ba_pct": total_ba * 100,
            "xyz_vol_24h": x["vol_24h"],
            "breakeven_h": breakeven_h,
        })

    opps.sort(key=lambda o: o["abs_spread"], reverse=True)
    return opps


def open_position(acct: PaperAccount, opp: dict, size_usd: float, leverage: int = DEFAULT_LEVERAGE) -> Position:
    total_entry_cost = calc_entry_cost(size_usd, opp)
    margin = size_usd / leverage

    pos = Position(
        ticker=opp["ticker"],
        bn_sym=opp["bn_sym"],
        direction=opp["direction"],
        size_usd=size_usd,
        margin_used=margin,
        leverage=leverage,
        xyz_entry_px=opp["xyz_px"],
        bn_entry_px=opp["bn_px"],
        entry_time=datetime.now(timezone.utc).isoformat(),
        entry_fees=total_entry_cost,
        entry_ts=time.time(),
    )

    acct.positions[opp["ticker"]] = pos
    acct.total_fees_paid += total_entry_cost
    acct.total_trades += 1
    log_trade("OPEN", opp["ticker"], size_usd, total_entry_cost, {
        "direction": opp["direction"],
        "spread_8h": opp["spread_8h"],
        "xyz_px": opp["xyz_px"],
        "bn_px": opp["bn_px"],
    })
    return pos


def close_position(acct: PaperAccount, ticker: str, mkt: dict, reason: str) -> float:
    pos = acct.positions.get(ticker)
    if not pos:
        return 0.0

    x = mkt["xyz"].get(ticker, {})
    b = mkt["bn"].get(pos.bn_sym, {})
    xyz_px = x.get("mark_px") or pos.xyz_entry_px
    bn_px = b.get("mark_px") or pos.bn_entry_px

    if xyz_px <= 0:
        xyz_px = pos.xyz_entry_px
    if bn_px <= 0:
        bn_px = pos.bn_entry_px

    x_oi = x.get("oi_usd", 0) if x else 0
    b_oi = b.get("oi_usd", 0) if b else 0
    xyz_slip = estimate_slippage(pos.size_usd, x_oi)
    bn_slip = estimate_slippage(pos.size_usd, b_oi)
    xyz_ba_half = x.get("ba_spread", 0) / 2 * LIMIT_ORDER_BA_FACTOR if x else 0.0005
    bn_ba_half = b.get("ba_spread", 0) / 2 * LIMIT_ORDER_BA_FACTOR if b else 0.0005
    exit_fees = pos.size_usd * (XYZ_TAKER_FEE + BN_TAKER_FEE + xyz_slip + bn_slip + xyz_ba_half + bn_ba_half)

    # 베이시스 PnL (델타뉴트럴: 양쪽 가격 괴리 변화분)
    if pos.direction == "XYZ_L_BN_S":
        basis_pnl = (
            (xyz_px - pos.xyz_entry_px) / pos.xyz_entry_px * pos.size_usd
            + (pos.bn_entry_px - bn_px) / pos.bn_entry_px * pos.size_usd
        )
    else:
        basis_pnl = (
            (pos.xyz_entry_px - xyz_px) / pos.xyz_entry_px * pos.size_usd
            + (bn_px - pos.bn_entry_px) / pos.bn_entry_px * pos.size_usd
        )

    net = pos.funding_pnl + basis_pnl - pos.entry_fees - exit_fees

    acct.total_fees_paid += exit_fees
    acct.total_realized_pnl += net
    acct.capital += net

    log_trade("CLOSE", ticker, pos.size_usd, exit_fees, {
        "reason": reason,
        "funding_pnl": pos.funding_pnl,
        "basis_pnl": basis_pnl,
        "entry_fees": pos.entry_fees,
        "exit_fees": exit_fees,
        "net_pnl": net,
        "hold_hours": pos.hold_hours(),
        "xyz_funding_n": pos.xyz_funding_count,
        "bn_funding_n": pos.bn_funding_count,
    })

    del acct.positions[ticker]
    return net


def apply_funding(acct: PaperAccount, mkt: dict, is_bn_settle: bool):
    """
    C1 수정: XYZ는 매시간, BN은 정산 시각에만 적용
    - XYZ: funding_1h × size_usd (매 iteration)
    - BN: funding_8h × size_usd (is_bn_settle=True일 때만)
    """
    for ticker, pos in list(acct.positions.items()):
        x = mkt["xyz"].get(ticker)
        b = mkt["bn"].get(pos.bn_sym)

        if not x or not b:
            pos.data_fail_streak += 1
            continue
        pos.data_fail_streak = 0

        xyz_fr_1h = x.get("funding_1h", 0)

        # XYZ 펀딩: 매시간 적용 (1h 레이트 그대로)
        if pos.direction == "XYZ_L_BN_S":
            xyz_pnl = -xyz_fr_1h * pos.size_usd  # 롱: 양수면 지불
        else:
            xyz_pnl = xyz_fr_1h * pos.size_usd    # 숏: 양수면 수취

        pos.funding_pnl += xyz_pnl
        pos.xyz_funding_count += 1
        acct.total_funding_earned += xyz_pnl

        # BN 펀딩: 8시간 정산 시각에만 적용
        if is_bn_settle:
            bn_fr_8h = b.get("funding_8h", 0)
            if pos.direction == "XYZ_L_BN_S":
                bn_pnl = bn_fr_8h * pos.size_usd   # 숏: 양수면 수취
            else:
                bn_pnl = -bn_fr_8h * pos.size_usd   # 롱: 양수면 지불

            pos.funding_pnl += bn_pnl
            pos.bn_funding_count += 1
            acct.total_funding_earned += bn_pnl

        # 미실현 베이시스 업데이트
        xyz_px = x.get("mark_px", pos.xyz_entry_px)
        bn_px = b.get("mark_px", pos.bn_entry_px)
        if xyz_px > 0 and bn_px > 0:
            if pos.direction == "XYZ_L_BN_S":
                pos.unrealized_basis = (
                    (xyz_px - pos.xyz_entry_px) / pos.xyz_entry_px * pos.size_usd
                    + (pos.bn_entry_px - bn_px) / pos.bn_entry_px * pos.size_usd
                )
            else:
                pos.unrealized_basis = (
                    (pos.xyz_entry_px - xyz_px) / pos.xyz_entry_px * pos.size_usd
                    + (bn_px - pos.bn_entry_px) / pos.bn_entry_px * pos.size_usd
                )


def should_close(pos: Position, mkt: dict, min_spread_pct: float) -> tuple:
    x = mkt["xyz"].get(pos.ticker)
    b = mkt["bn"].get(pos.bn_sym)

    if pos.data_fail_streak >= 5:
        return True, "DATA_FAIL"

    if not x or not b:
        return False, ""

    x_fr = x.get("funding_1h", 0)
    b_fr = b.get("funding_8h", 0)
    current_spread = x_fr * 8 - b_fr

    # 스프레드 완전 반전 → 즉시 청산 (보유시간 무관)
    if pos.direction == "XYZ_L_BN_S" and current_spread > 0:
        return True, "SPREAD_REVERSED"
    if pos.direction == "XYZ_S_BN_L" and current_spread < 0:
        return True, "SPREAD_REVERSED"

    # 최소 보유시간 미달이면 반전 외에는 보유
    if pos.hold_hours() < MIN_HOLD_HOURS:
        return False, ""

    # 스프레드 축소 (최소 기준의 20% 이하, MIN_HOLD 이후만)
    if abs(current_spread) < min_spread_pct / 100 * 0.2:
        return True, "SPREAD_COLLAPSED"

    # 이익 실현: 수수료 2배 이상 벌었으면 확보
    if pos.net_pnl() > pos.entry_fees * 2:
        return True, "TAKE_PROFIT"

    # 48시간 보유 후 순손실이면 손절
    if pos.hold_hours() >= 48 and pos.net_pnl() < -pos.entry_fees * 0.3:
        return True, "STOP_LOSS_TIME"

    return False, ""


def log_trade(action: str, ticker: str, size: float, fees: float, extra: dict):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "ticker": ticker,
        "size_usd": round(size, 2),
        "fees": round(fees, 4),
    }
    for k, v in extra.items():
        entry[k] = round(v, 6) if isinstance(v, float) else v
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_pnl(acct: PaperAccount):
    unrealized = sum(p.net_pnl() for p in acct.positions.values())
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "capital": round(acct.capital, 2),
        "total_pnl_pct": round((acct.capital + unrealized) / acct.initial_capital * 100 - 100, 4),
        "realized_pnl": round(acct.total_realized_pnl, 4),
        "unrealized": round(unrealized, 4),
        "total_fees": round(acct.total_fees_paid, 4),
        "total_funding": round(acct.total_funding_earned, 4),
        "net_funding_minus_fees": round(acct.total_funding_earned - acct.total_fees_paid, 4),
        "open_positions": len(acct.positions),
        "total_trades": acct.total_trades,
    }
    with open(PNL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def print_status(acct: PaperAccount, mkt: dict):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    unrealized = sum(p.net_pnl() for p in acct.positions.values())
    total_equity = acct.capital + unrealized
    pnl_pct = total_equity / acct.initial_capital * 100 - 100

    print(f"\n{'='*95}")
    print(f"  PAPER TRADING STATUS  |  {now_str}")
    print(f"{'='*95}")
    print(f"  Capital:   ${acct.capital:,.2f}  (initial: ${acct.initial_capital:,.2f})")
    print(f"  Equity:    ${total_equity:,.2f}  ({pnl_pct:+.4f}%)")
    print(f"  Realized:  ${acct.total_realized_pnl:+,.4f}")
    print(f"  Unrealized: ${unrealized:+,.4f}")
    print(f"  Fees paid:  ${acct.total_fees_paid:,.4f}")
    print(f"  Funding:    ${acct.total_funding_earned:+,.4f}")
    print(f"  Net (fund-fees): ${acct.total_funding_earned - acct.total_fees_paid:+,.4f}")
    print(f"  Trades: {acct.total_trades}  |  Open: {len(acct.positions)}")

    if acct.positions:
        print(f"\n  {'─'*90}")
        print(f"  Open Positions")
        print(f"  {'─'*90}")
        rows = []
        for ticker, pos in acct.positions.items():
            x = mkt["xyz"].get(ticker, {})
            b = mkt["bn"].get(pos.bn_sym, {})
            x_fr = x.get("funding_1h", 0) * 8
            b_fr = b.get("funding_8h", 0)
            cur_spread = (x_fr - b_fr) * 100

            dir_short = "L/S" if "L_BN_S" in pos.direction else "S/L"
            rows.append([
                ticker,
                dir_short,
                f"${pos.size_usd:,.0f}({pos.leverage}x)",
                f"{pos.hold_hours():.1f}h",
                f"{pos.xyz_funding_count}/{pos.bn_funding_count}",
                f"${pos.funding_pnl:+,.4f}",
                f"${pos.unrealized_basis:+,.2f}",
                f"${-pos.entry_fees:,.2f}",
                f"${pos.net_pnl():+,.4f}",
                f"{cur_spread:+.4f}%",
            ])
        headers = ["Ticker", "XYZ/BN", "Size", "Hold", "Fund(X/B)", "Funding$", "Basis", "Fees", "Net", "Spread"]
        print(tabulate(rows, headers=headers, tablefmt="simple_grid", stralign="right"))


def print_scan_results(opps: list):
    if not opps:
        print("  No opportunities found")
        return
    rows = []
    for o in opps[:8]:
        entry_cost = calc_entry_cost(2000, o)
        rows.append([
            o["ticker"],
            f"{o['spread_8h']*100:+.4f}%",
            f"{o['annual']:+.1f}%",
            "L/S" if "L_BN_S" in o["direction"] else "S/L",
            f"${o['min_oi_usd']/1e6:.1f}M",
            f"${o['xyz_vol_24h']/1e6:.0f}M",
            f"{o['total_ba_pct']:.3f}%",
            f"${entry_cost:.2f}",
            f"{o.get('breakeven_h', 0):.1f}h",
        ])
    headers = ["Ticker", "Spread/8h", "Annual", "XYZ/BN", "MinOI", "Vol24h", "BA%", "Cost$2K", "BEven"]
    print(tabulate(rows, headers=headers, tablefmt="simple_grid", stralign="right"))


def main():
    args = sys.argv[1:]
    capital = 10000.0
    max_pos = 2
    leverage = DEFAULT_LEVERAGE
    min_spread = 0.10       # 0.10%/8h (이전 0.04 → 비용 대비 충분한 스프레드만)
    min_oi = 0.8             # 최소 OI $800K
    pos_size_pct = 0.30      # 마진의 30%씩 (집중 투자)

    i = 0
    while i < len(args):
        if args[i] == "--capital" and i+1 < len(args):
            capital = float(args[i+1]); i += 2
        elif args[i] == "--max-pos" and i+1 < len(args):
            max_pos = int(args[i+1]); i += 2
        elif args[i] == "--leverage" and i+1 < len(args):
            leverage = int(args[i+1]); i += 2
        elif args[i] == "--min-spread" and i+1 < len(args):
            min_spread = float(args[i+1]); i += 2
        elif args[i] == "--min-oi" and i+1 < len(args):
            min_oi = float(args[i+1]); i += 2
        elif args[i] == "--size-pct" and i+1 < len(args):
            pos_size_pct = float(args[i+1]); i += 2
        elif args[i] == "--reset":
            for f in [STATE_FILE, TRADES_FILE, PNL_FILE]:
                if os.path.exists(f):
                    os.remove(f)
            print("State reset."); i += 1
        else:
            i += 1

    acct = PaperAccount.load(default_capital=capital)
    iteration = 0

    print(f"Tradenance Paper Trader (1h cycle)")
    print(f"  Capital: ${acct.capital:,.2f} | Leverage: {leverage}x | Max positions: {max_pos}")
    print(f"  Min spread: {min_spread}%/8h | Min OI: ${min_oi}M")
    print(f"  Margin per pos: {pos_size_pct*100:.0f}% of avail → notional ×{leverage}")
    print(f"  Fees: XYZ {XYZ_TAKER_FEE*100:.3f}% + BN {BN_TAKER_FEE*100:.3f}% | Slip safety: {SLIPPAGE_SAFETY}x | BA limit: {LIMIT_ORDER_BA_FACTOR}x")
    print(f"  Max BA: {MAX_BA_PCT*100:.2f}% | Max breakeven: {MAX_BREAKEVEN_HOURS}h | Min hold: {MIN_HOLD_HOURS}h")

    while True:
        iteration += 1
        now = datetime.now(timezone.utc)
        bn_settle = is_bn_settlement_hour(now.hour)

        try:
            print(f"\n{'#'*95}")
            print(f"  Iteration {iteration}  |  {now.strftime('%Y-%m-%d %H:%M UTC')}"
                  f"  |  BN Settlement: {'YES' if bn_settle else 'no'}")
            print(f"{'#'*95}")

            mkt = fetch_market_data()

            # 1. 보유 포지션에 펀딩 적용
            if acct.positions:
                print(f"\n  [1/4] Applying funding (XYZ: 1h, BN: {'8h SETTLE' if bn_settle else 'skip'})...")
                apply_funding(acct, mkt, bn_settle)
                for t, p in acct.positions.items():
                    print(f"    {t}: funding=${p.funding_pnl:+,.4f} (XYZ×{p.xyz_funding_count} BN×{p.bn_funding_count})")
            else:
                print(f"\n  [1/4] No positions to fund")

            # 2. 청산 판단
            closed = []
            for ticker in list(acct.positions.keys()):
                should, reason = should_close(acct.positions[ticker], mkt, min_spread)
                if should:
                    net = close_position(acct, ticker, mkt, reason)
                    closed.append((ticker, reason, net))
                    print(f"  [CLOSE] {ticker} | {reason} | Net: ${net:+,.4f}")
            if not closed:
                print(f"  [2/4] No positions to close")

            # 3. 기회 탐색
            opps = scan_opportunities(mkt, min_spread, min_oi)
            existing = set(acct.positions.keys())
            new_opps = [o for o in opps if o["ticker"] not in existing]
            print(f"  [3/4] Opportunities: {len(new_opps)} new / {len(opps)} total")
            print_scan_results(new_opps)

            # 4. 신규 진입 (유동성 비례 사이징 + 레버리지)
            slots = max_pos - len(acct.positions)
            entered = 0
            if slots > 0 and new_opps:
                for opp in new_opps[:slots]:
                    size = calc_position_size(acct.available_capital(), pos_size_pct, opp["min_oi_usd"], leverage)
                    if size < 100:
                        print(f"  [4/4] Insufficient capital or liquidity (avail=${acct.available_capital():.0f}, minOI=${opp['min_oi_usd']/1e6:.1f}M)")
                        break
                    pos = open_position(acct, opp, size, leverage)
                    dir_short = "L/S" if "L_BN_S" in pos.direction else "S/L"
                    margin = size / leverage
                    print(f"  [OPEN] {pos.ticker} | XYZ/BN={dir_short} | ${size:,.0f}({leverage}x, margin=${margin:,.0f}) | BA={opp['total_ba_pct']:.3f}% | Fees: ${pos.entry_fees:.4f}")
                    entered += 1

            if entered == 0 and slots > 0:
                print(f"  [4/4] No entries (slots={slots})")

            # 상태 출력/저장
            print_status(acct, mkt)
            log_pnl(acct)
            acct.save()

        except KeyboardInterrupt:
            print("\nStopped.")
            acct.save()
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()

        next_str = (datetime.now(timezone.utc)).strftime("%H:%M")
        print(f"\n  [{next_str}] Next in {INTERVAL_MIN}m... (Ctrl+C to stop)")
        try:
            time.sleep(INTERVAL_MIN * 60)
        except KeyboardInterrupt:
            print("\nStopped.")
            acct.save()
            break

    print("\nFinal:")
    try:
        mkt = fetch_market_data()
        print_status(acct, mkt)
    except Exception:
        pass


if __name__ == "__main__":
    main()
