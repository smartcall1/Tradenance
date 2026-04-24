"""
Tradenance Paper Trader — 실비용 반영 펀딩비 아비트라지 시뮬레이터

실제 거래와 동일한 비용 구조 반영:
  - 진입/청산 수수료 (taker/maker)
  - 슬리피지 추정 (포지션 크기 vs OI 기반)
  - 가격 괴리 (basis) — 양쪽 mark price 차이
  - 펀딩비 수취/지불 — 실시간 레이트 적용
  - 로테이션 비용 — 종목 교체 시 양쪽 청산+재진입

사용법:
  python paper_trader.py                         # 기본 (자본 $10K, 최대 3포지션, 8시간 주기)
  python paper_trader.py --capital 5000          # 자본 $5K
  python paper_trader.py --max-pos 5             # 최대 5포지션
  python paper_trader.py --interval 60           # 60분 주기
  python paper_trader.py --min-spread 0.03       # 최소 스프레드 0.03%/8h
  python paper_trader.py --min-oi 2              # 최소 OI $2M
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

# 수수료 설정 (taker 기준, worst case)
XYZ_TAKER_FEE = 0.00009    # 0.009% Growth Mode
XYZ_MAKER_FEE = -0.00003   # -0.003% rebate
BN_TAKER_FEE = 0.0005      # 0.05%
BN_MAKER_FEE = 0.0002      # 0.02%

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
    """OI 대비 포지션 비율로 슬리피지 추정"""
    if oi_usd <= 0:
        return 0.005
    ratio = notional / oi_usd
    if ratio < 0.005:
        return 0.0001
    elif ratio < 0.01:
        return 0.0003
    elif ratio < 0.02:
        return 0.0005
    elif ratio < 0.05:
        return 0.001
    elif ratio < 0.10:
        return 0.002
    else:
        return 0.005


@dataclass
class Position:
    ticker: str
    bn_sym: str
    direction: str          # "XYZ_L_BN_S" or "XYZ_S_BN_L"
    size_usd: float         # 한쪽 기준 노셔널
    xyz_entry_px: float
    bn_entry_px: float
    entry_time: str
    entry_fees: float       # 진입 시 총 비용 (양쪽 수수료 + 슬리피지)
    funding_pnl: float = 0.0
    funding_count: int = 0
    unrealized_basis: float = 0.0

    def net_pnl(self) -> float:
        return self.funding_pnl - self.entry_fees + self.unrealized_basis


@dataclass
class PaperAccount:
    initial_capital: float = 10000.0
    capital: float = 10000.0
    positions: dict = field(default_factory=dict)
    total_realized_pnl: float = 0.0
    total_fees_paid: float = 0.0
    total_funding_earned: float = 0.0
    total_trades: int = 0
    trade_log: list = field(default_factory=list)

    def available_capital(self) -> float:
        used = sum(p.size_usd for p in self.positions.values())
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
    """양쪽 거래소 데이터 한번에 조회"""
    # Trade.xyz
    resp = requests.post(HL_API, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    xyz = {}
    for meta, ctx in zip(data[0]["universe"], data[1]):
        name = meta["name"].replace("xyz:", "")
        xyz[name] = {
            "funding_1h": _sf(ctx.get("funding")),
            "funding_8h": _sf(ctx.get("funding")) * 8,
            "mark_px": _sf(ctx.get("markPx")),
            "oi": _sf(ctx.get("openInterest")),
            "vol_24h": _sf(ctx.get("dayNtlVlm")),
        }

    # Binance
    bn = {}
    for bn_sym in OVERLAP_PAIRS.values():
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                             params={"symbol": bn_sym}, timeout=10)
            if r.status_code == 200:
                d = r.json()
                bn[bn_sym] = {
                    "funding_8h": _sf(d.get("lastFundingRate")),
                    "mark_px": _sf(d.get("markPrice")),
                }
        except:
            pass
        time.sleep(0.08)

    for bn_sym in OVERLAP_PAIRS.values():
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                             params={"symbol": bn_sym}, timeout=10)
            if r.status_code == 200 and bn_sym in bn:
                bn[bn_sym]["oi"] = _sf(r.json().get("openInterest"))
                bn[bn_sym]["oi_usd"] = bn[bn_sym]["oi"] * bn[bn_sym]["mark_px"]
        except:
            pass
        time.sleep(0.05)

    return {"xyz": xyz, "bn": bn}


def scan_opportunities(mkt: dict, min_spread: float, min_oi: float) -> list:
    """스프레드 기준 기회 탐색, 유동성 필터 적용"""
    opps = []
    for ticker, bn_sym in OVERLAP_PAIRS.items():
        x = mkt["xyz"].get(ticker)
        b = mkt["bn"].get(bn_sym)
        if not x or not b:
            continue

        x8 = x["funding_8h"]
        b8 = b["funding_8h"]
        spread = x8 - b8
        abs_spread = abs(spread)

        x_oi_usd = x["oi"] * x["mark_px"] if x["mark_px"] else 0
        b_oi_usd = b.get("oi_usd", 0)
        min_oi_usd = min(x_oi_usd, b_oi_usd)

        if abs_spread < min_spread / 100:
            continue
        if min_oi_usd < min_oi * 1e6:
            continue

        direction = "XYZ_S_BN_L" if spread > 0 else "XYZ_L_BN_S"

        opps.append({
            "ticker": ticker,
            "bn_sym": bn_sym,
            "spread": spread,
            "abs_spread": abs_spread,
            "annual": spread * 3 * 365 * 100,
            "direction": direction,
            "xyz_px": x["mark_px"],
            "bn_px": b["mark_px"],
            "xyz_funding_1h": x["funding_1h"],
            "bn_funding_8h": b8,
            "xyz_oi_usd": x_oi_usd,
            "bn_oi_usd": b_oi_usd,
            "min_oi_usd": min_oi_usd,
        })

    opps.sort(key=lambda o: o["abs_spread"], reverse=True)
    return opps


def open_position(acct: PaperAccount, opp: dict, size_usd: float) -> Position:
    """페이퍼 포지션 진입 — 수수료+슬리피지 계산"""
    xyz_slip = estimate_slippage(size_usd, opp["xyz_oi_usd"])
    bn_slip = estimate_slippage(size_usd, opp["bn_oi_usd"])

    xyz_fee = size_usd * XYZ_TAKER_FEE
    bn_fee = size_usd * BN_TAKER_FEE
    xyz_slip_cost = size_usd * xyz_slip
    bn_slip_cost = size_usd * bn_slip

    total_entry_cost = xyz_fee + bn_fee + xyz_slip_cost + bn_slip_cost

    pos = Position(
        ticker=opp["ticker"],
        bn_sym=opp["bn_sym"],
        direction=opp["direction"],
        size_usd=size_usd,
        xyz_entry_px=opp["xyz_px"],
        bn_entry_px=opp["bn_px"],
        entry_time=datetime.now(timezone.utc).isoformat(),
        entry_fees=total_entry_cost,
    )

    acct.positions[opp["ticker"]] = pos
    acct.total_fees_paid += total_entry_cost
    acct.total_trades += 1

    log_trade(acct, "OPEN", opp["ticker"], size_usd, total_entry_cost, opp)
    return pos


def close_position(acct: PaperAccount, ticker: str, mkt: dict, reason: str) -> float:
    """포지션 청산 — 청산 비용 + 베이시스 PnL 확정"""
    pos = acct.positions.get(ticker)
    if not pos:
        return 0.0

    bn_sym = pos.bn_sym
    x = mkt["xyz"].get(ticker, {})
    b = mkt["bn"].get(bn_sym, {})
    xyz_px = x.get("mark_px", pos.xyz_entry_px)
    bn_px = b.get("mark_px", pos.bn_entry_px)

    xyz_slip = estimate_slippage(pos.size_usd, x.get("oi", 0) * xyz_px if xyz_px else 0)
    bn_slip = estimate_slippage(pos.size_usd, b.get("oi_usd", 0))

    exit_fees = pos.size_usd * (XYZ_TAKER_FEE + BN_TAKER_FEE) + pos.size_usd * (xyz_slip + bn_slip)

    # 베이시스 PnL: 가격 변동에 의한 양쪽 차이
    # 델타뉴트럴이므로 가격 변동 자체는 상쇄되지만, 양쪽 가격 괴리(basis) 변화분이 PnL
    if pos.direction == "XYZ_L_BN_S":
        xyz_pnl = (xyz_px - pos.xyz_entry_px) / pos.xyz_entry_px * pos.size_usd
        bn_pnl = (pos.bn_entry_px - bn_px) / pos.bn_entry_px * pos.size_usd
    else:
        xyz_pnl = (pos.xyz_entry_px - xyz_px) / pos.xyz_entry_px * pos.size_usd
        bn_pnl = (bn_px - pos.bn_entry_px) / pos.bn_entry_px * pos.size_usd

    basis_pnl = xyz_pnl + bn_pnl
    net = pos.funding_pnl + basis_pnl - pos.entry_fees - exit_fees

    acct.total_fees_paid += exit_fees
    acct.total_realized_pnl += net
    acct.capital += net

    log_trade(acct, "CLOSE", ticker, pos.size_usd, exit_fees, {
        "reason": reason,
        "funding_pnl": pos.funding_pnl,
        "basis_pnl": basis_pnl,
        "total_fees": pos.entry_fees + exit_fees,
        "net_pnl": net,
        "held_periods": pos.funding_count,
    })

    del acct.positions[ticker]
    return net


def apply_funding(acct: PaperAccount, mkt: dict):
    """현재 펀딩레이트를 보유 포지션에 적용"""
    for ticker, pos in list(acct.positions.items()):
        x = mkt["xyz"].get(ticker, {})
        b = mkt["bn"].get(pos.bn_sym, {})

        xyz_fr = x.get("funding_1h", 0)
        bn_fr_8h = b.get("funding_8h", 0)

        # Trade.xyz: 1시간 정산이지만 8시간 주기로 돌리므로 8배
        # 실제로는 마지막 8시간 동안의 실제 레이트 합산이 정확하지만
        # 현재 레이트 × 8로 추정 (보수적)
        xyz_funding_8h = xyz_fr * 8

        # 방향에 따른 펀딩 PnL
        if pos.direction == "XYZ_L_BN_S":
            # XYZ LONG: 양수 펀딩이면 롱이 지불, 음수면 롱이 수취
            xyz_pnl = -xyz_funding_8h * pos.size_usd
            # BN SHORT: 양수 펀딩이면 숏이 수취, 음수면 숏이 지불
            bn_pnl = bn_fr_8h * pos.size_usd
        else:  # XYZ_S_BN_L
            xyz_pnl = xyz_funding_8h * pos.size_usd
            bn_pnl = -bn_fr_8h * pos.size_usd

        period_funding = xyz_pnl + bn_pnl
        pos.funding_pnl += period_funding
        pos.funding_count += 1
        acct.total_funding_earned += period_funding

        # 미실현 베이시스 업데이트
        xyz_px = x.get("mark_px", pos.xyz_entry_px)
        bn_px = b.get("mark_px", pos.bn_entry_px)
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


def should_close(pos: Position, mkt: dict, min_spread: float) -> tuple[bool, str]:
    """포지션 청산 판단"""
    x = mkt["xyz"].get(pos.ticker, {})
    b = mkt["bn"].get(pos.bn_sym, {})
    if not x or not b:
        return False, ""

    current_spread = x.get("funding_8h", 0) - b.get("funding_8h", 0)

    # 스프레드 반전: 방향이 바뀌면 청산
    if pos.direction == "XYZ_L_BN_S" and current_spread > 0:
        return True, "SPREAD_REVERSED"
    if pos.direction == "XYZ_S_BN_L" and current_spread < 0:
        return True, "SPREAD_REVERSED"

    # 스프레드 축소: 최소 기준 이하로 줄면 청산
    if abs(current_spread) < min_spread / 100 * 0.3:
        return True, "SPREAD_COLLAPSED"

    # 누적 PnL이 진입비용도 못 벌면 5주기 후 손절
    if pos.funding_count >= 5 and pos.net_pnl() < -pos.entry_fees:
        return True, "STOP_LOSS"

    return False, ""


def log_trade(acct: PaperAccount, action: str, ticker: str, size: float, fees: float, extra: dict):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "ticker": ticker,
        "size_usd": round(size, 2),
        "fees": round(fees, 4),
        **{k: round(v, 4) if isinstance(v, float) else v for k, v in extra.items()},
    }
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_pnl(acct: PaperAccount):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "capital": round(acct.capital, 2),
        "pnl_pct": round((acct.capital / acct.initial_capital - 1) * 100, 4),
        "realized_pnl": round(acct.total_realized_pnl, 4),
        "unrealized": round(sum(p.net_pnl() for p in acct.positions.values()), 4),
        "total_fees": round(acct.total_fees_paid, 4),
        "total_funding": round(acct.total_funding_earned, 4),
        "open_positions": len(acct.positions),
        "total_trades": acct.total_trades,
    }
    with open(PNL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def print_status(acct: PaperAccount, mkt: dict):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    unrealized = sum(p.net_pnl() for p in acct.positions.values())
    total_pnl = acct.total_realized_pnl + unrealized
    pnl_pct = (acct.capital + unrealized) / acct.initial_capital * 100 - 100

    print(f"\n{'='*90}")
    print(f"  PAPER TRADING STATUS  |  {now_str}")
    print(f"{'='*90}")
    print(f"  Capital: ${acct.capital:,.2f}  (initial: ${acct.initial_capital:,.2f})")
    print(f"  Realized PnL: ${acct.total_realized_pnl:+,.4f}")
    print(f"  Unrealized:   ${unrealized:+,.4f}")
    print(f"  Total PnL:    ${total_pnl:+,.4f}  ({pnl_pct:+.4f}%)")
    print(f"  Fees paid:    ${acct.total_fees_paid:,.4f}")
    print(f"  Funding earned: ${acct.total_funding_earned:+,.4f}")
    print(f"  Net (funding - fees): ${acct.total_funding_earned - acct.total_fees_paid:+,.4f}")
    print(f"  Trades: {acct.total_trades}  |  Open: {len(acct.positions)}")

    if acct.positions:
        print(f"\n  {'─'*85}")
        print(f"  Open Positions")
        print(f"  {'─'*85}")
        rows = []
        for ticker, pos in acct.positions.items():
            x = mkt["xyz"].get(ticker, {})
            b = mkt["bn"].get(pos.bn_sym, {})
            cur_spread = (x.get("funding_8h", 0) - b.get("funding_8h", 0)) * 100

            rows.append([
                ticker,
                pos.direction.replace("_", " "),
                f"${pos.size_usd:,.0f}",
                f"{pos.funding_count}",
                f"${pos.funding_pnl:+,.4f}",
                f"${pos.unrealized_basis:+,.4f}",
                f"${-pos.entry_fees:,.4f}",
                f"${pos.net_pnl():+,.4f}",
                f"{cur_spread:+.4f}%",
            ])
        headers = ["Ticker", "Dir", "Size", "Periods", "Funding", "Basis", "Fees", "Net", "CurSpread"]
        print(tabulate(rows, headers=headers, tablefmt="simple_grid", stralign="right"))


def main():
    args = sys.argv[1:]
    capital = 10000.0
    max_pos = 3
    interval_min = 480       # 8시간 = 480분 (바이낸스 펀딩 주기)
    min_spread = 0.03        # 최소 스프레드 0.03%/8h
    min_oi = 1.0             # 최소 OI $1M
    pos_size_pct = 0.25      # 자본의 25%씩

    i = 0
    while i < len(args):
        if args[i] == "--capital" and i+1 < len(args):
            capital = float(args[i+1]); i += 2
        elif args[i] == "--max-pos" and i+1 < len(args):
            max_pos = int(args[i+1]); i += 2
        elif args[i] == "--interval" and i+1 < len(args):
            interval_min = int(args[i+1]); i += 2
        elif args[i] == "--min-spread" and i+1 < len(args):
            min_spread = float(args[i+1]); i += 2
        elif args[i] == "--min-oi" and i+1 < len(args):
            min_oi = float(args[i+1]); i += 2
        elif args[i] == "--size-pct" and i+1 < len(args):
            pos_size_pct = float(args[i+1]); i += 2
        elif args[i] == "--reset":
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
            print("State reset."); i += 1
        else:
            i += 1

    acct = PaperAccount.load(default_capital=capital)
    iteration = 0

    print(f"Tradenance Paper Trader")
    print(f"  Capital: ${acct.capital:,.2f} | Max positions: {max_pos}")
    print(f"  Interval: {interval_min}m | Min spread: {min_spread}%/8h | Min OI: ${min_oi}M")
    print(f"  Position size: {pos_size_pct*100:.0f}% of capital")

    while True:
        iteration += 1
        try:
            print(f"\n{'#'*90}")
            print(f"  Iteration {iteration}  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"{'#'*90}")

            mkt = fetch_market_data()

            # 1. 보유 포지션에 펀딩 적용
            if acct.positions:
                print(f"\n  [1/4] Applying funding to {len(acct.positions)} positions...")
                apply_funding(acct, mkt)

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
            existing_tickers = set(acct.positions.keys())
            new_opps = [o for o in opps if o["ticker"] not in existing_tickers]

            print(f"  [3/4] Found {len(new_opps)} opportunities (filtered from {len(opps)} total)")
            if new_opps:
                for o in new_opps[:5]:
                    print(f"    {o['ticker']:>8s} | Spread {o['spread']*100:+.4f}%/8h | Ann {o['annual']:+.1f}% | MinOI ${o['min_oi_usd']/1e6:.1f}M")

            # 4. 신규 진입
            slots = max_pos - len(acct.positions)
            if slots > 0 and new_opps:
                size = acct.available_capital() * pos_size_pct
                size = min(size, 5000)  # 최대 $5K per leg (유동성 보수적)

                for opp in new_opps[:slots]:
                    if size < 100:
                        break
                    pos = open_position(acct, opp, size)
                    dir_str = "L" if "L_BN_S" in pos.direction else "S"
                    print(f"  [OPEN] {pos.ticker} | XYZ {dir_str} + BN {'S' if dir_str=='L' else 'L'} | ${size:,.0f} | Fees: ${pos.entry_fees:,.4f}")
            else:
                print(f"  [4/4] No new entries (slots={slots}, opps={len(new_opps)})")

            # 상태 출력 및 저장
            print_status(acct, mkt)
            log_pnl(acct)
            acct.save()

        except KeyboardInterrupt:
            print("\nStopped by user.")
            acct.save()
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()

        print(f"\n  Next iteration in {interval_min}m... (Ctrl+C to stop)")
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            acct.save()
            break

    print("\nFinal status:")
    try:
        mkt = fetch_market_data()
        print_status(acct, mkt)
    except:
        pass
    print("Done.")


if __name__ == "__main__":
    main()
