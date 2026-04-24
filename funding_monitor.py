"""
Trade.xyz vs Binance 주식/ETF 퍼프 펀딩레이트 아비트라지 모니터

Trade.xyz (Hyperliquid HIP-3)는 0.5x 펀딩 승수를 적용하여
Binance TRADIFI_PERPETUAL 대비 구조적 펀딩레이트 차이가 발생.
이 스크립트는 교차 상장 종목들의 펀딩레이트 스프레드를 실시간 추적.

사용법:
  python funding_monitor.py                  # 현재 스냅샷 + TOP3 히스토리
  python funding_monitor.py --loop 60        # 60분마다 반복 스캔
  python funding_monitor.py --history 14     # 14일 히스토리 분석
  python funding_monitor.py --top 5          # TOP 5 히스토리 분석
  python funding_monitor.py --loop 30 --top 5 --history 14
"""

import requests
import time
import json
import sys
import os
from datetime import datetime, timezone
from tabulate import tabulate

OVERLAP_PAIRS = {
    # 주식
    "AAPL": "AAPLUSDT",
    "AMZN": "AMZNUSDT",
    "BABA": "BABAUSDT",
    "COIN": "COINUSDT",
    "CRCL": "CRCLUSDT",
    "GOOGL": "GOOGLUSDT",
    "HOOD": "HOODUSDT",
    "INTC": "INTCUSDT",
    "META": "METAUSDT",
    "MSFT": "MSFTUSDT",
    "MSTR": "MSTRUSDT",
    "MU": "MUUSDT",
    "NVDA": "NVDAUSDT",
    "PLTR": "PLTRUSDT",
    "SNDK": "SNDKUSDT",
    "TSLA": "TSLAUSDT",
    "TSM": "TSMUSDT",
    # ETF
    "EWJ": "EWJUSDT",
    "EWY": "EWYUSDT",
    # 원자재
    "CL": "CLUSDT",
    "COPPER": "COPPERUSDT",
    "NATGAS": "NATGASUSDT",
    "GOLD": "XAUUSDT",
    "SILVER": "XAGUSDT",
    "PLATINUM": "XPTUSDT",
    "BRENTOIL": "BZUSDT",
}

HL_API = "https://api.hyperliquid.xyz/info"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SNAPSHOT_FILE = os.path.join(DATA_DIR, "funding_snapshot.jsonl")
SPREAD_HISTORY_FILE = os.path.join(DATA_DIR, "spread_history.jsonl")


def _sf(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def fetch_tradexyz_all() -> dict:
    resp = requests.post(HL_API, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    meta_list, ctx_list = data[0]["universe"], data[1]

    result = {}
    for meta, ctx in zip(meta_list, ctx_list):
        name = meta["name"].replace("xyz:", "")
        funding_1h = _sf(ctx.get("funding"))
        result[name] = {
            "funding_1h": funding_1h,
            "funding_8h": funding_1h * 8,
            "mark_px": _sf(ctx.get("markPx")),
            "oracle_px": _sf(ctx.get("oraclePx")),
            "oi": _sf(ctx.get("openInterest")),
            "vol_24h": _sf(ctx.get("dayNtlVlm")),
            "premium": _sf(ctx.get("premium")),
        }
    return result


def fetch_binance_all() -> dict:
    result = {}
    bn_symbols = set(OVERLAP_PAIRS.values())

    r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10)
    r.raise_for_status()
    for d in r.json():
        sym = d.get("symbol", "")
        if sym in bn_symbols:
            result[sym] = {
                "funding_8h": _sf(d.get("lastFundingRate")),
                "mark_px": _sf(d.get("markPrice")),
                "index_px": _sf(d.get("indexPrice")),
            }

    for bn_sym in bn_symbols:
        if bn_sym not in result:
            continue
        try:
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": bn_sym}, timeout=10,
            )
            if r.status_code == 200:
                result[bn_sym]["oi"] = _sf(r.json().get("openInterest"))
        except Exception:
            pass
        time.sleep(0.05)

    return result


def fetch_xyz_history(coin: str, hours: int = 168) -> list:
    start_ms = int((time.time() - hours * 3600) * 1000)
    r = requests.post(
        HL_API,
        json={"type": "fundingHistory", "coin": f"xyz:{coin}", "startTime": start_ms},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_bn_history(symbol: str, limit: int = 100) -> list:
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        params={"symbol": symbol, "limit": limit}, timeout=10,
    )
    r.raise_for_status()
    return r.json()


def compare_current():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*105}")
    print(f"  Trade.xyz vs Binance Funding Rate Monitor  |  {now_str}")
    print(f"{'='*105}")

    xyz = fetch_tradexyz_all()
    bn = fetch_binance_all()

    rows = []
    for ticker, bn_sym in OVERLAP_PAIRS.items():
        x = xyz.get(ticker)
        b = bn.get(bn_sym)
        if not x or not b or "error" in b:
            continue

        x8 = x["funding_8h"]
        b8 = b["funding_8h"]
        spread = x8 - b8
        ann = spread * 3 * 365 * 100

        x_oi_usd = x["oi"] * x["mark_px"] if x["mark_px"] else 0
        b_oi_usd = b.get("oi", 0) * b["mark_px"] if b["mark_px"] else 0
        min_oi = min(x_oi_usd, b_oi_usd)

        if abs(spread) < 1e-7:
            direction = "  -"
        elif spread > 0:
            direction = "XYZ S + BN L"
        else:
            direction = "XYZ L + BN S"

        rows.append([
            ticker,
            f"{x8*100:+.4f}%",
            f"{b8*100:+.4f}%",
            f"{spread*100:+.4f}%",
            f"{ann:+.1f}%",
            direction,
            f"${x_oi_usd/1e6:.1f}M",
            f"${b_oi_usd/1e6:.1f}M",
            f"${x['vol_24h']/1e6:.1f}M",
            f"${min_oi/1e6:.1f}M",
        ])

    rows.sort(key=lambda r: abs(float(r[4].rstrip('%').lstrip('+'))), reverse=True)

    headers = [
        "Ticker", "XYZ 8h", "BN 8h", "Spread", "Annual",
        "Direction", "XYZ OI", "BN OI", "XYZ Vol", "MinOI",
    ]
    print(tabulate(rows, headers=headers, tablefmt="simple_grid", stralign="right"))

    print(f"\n  {len(rows)} pairs compared")
    print("  Spread = XYZ - BN | (+) short XYZ + long BN | (-) long XYZ + short BN")
    print("  MinOI = min(XYZ_OI, BN_OI) -- practical capacity limit")

    top = [r for r in rows if r[5].strip() != "-"][:5]
    if top:
        print(f"\n  {'─'*70}")
        print(f"  TOP 5 Spread Opportunities")
        print(f"  {'─'*70}")
        for i, r in enumerate(top, 1):
            print(f"  {i}. {r[0]:>8s} | Spread {r[3]:>10s} | Annual {r[4]:>10s} | MinOI {r[9]:>7s} | {r[5]}")

    return rows


def historical_analysis(ticker: str, bn_sym: str, days: int = 7):
    print(f"\n  {'─'*75}")
    print(f"  {ticker} Historical Analysis ({days}d)")
    print(f"  {'─'*75}")

    xh = fetch_xyz_history(ticker, hours=days * 24)
    bh = fetch_bn_history(bn_sym, limit=days * 3 + 5)

    if not xh:
        print(f"    Trade.xyz: no data")
        return {}
    if not bh:
        print(f"    Binance: no data")
        return {}

    # Trade.xyz 1h -> 8h 블록 집계
    x_8h_blocks = []
    for i in range(0, len(xh) - 7, 8):
        chunk = xh[i:i+8]
        rate_sum = sum(_sf(r.get("fundingRate")) for r in chunk)
        x_8h_blocks.append(rate_sum)

    b_rates = [_sf(r.get("fundingRate")) for r in bh]

    stats = {}

    if x_8h_blocks:
        avg_x = sum(x_8h_blocks) / len(x_8h_blocks)
        min_x, max_x = min(x_8h_blocks), max(x_8h_blocks)
        print(f"    XYZ  avg={avg_x*100:+.4f}%/8h  min={min_x*100:+.4f}%  max={max_x*100:+.4f}%  ann={avg_x*3*365*100:+.1f}%  n={len(x_8h_blocks)}")
        stats["xyz_avg"] = avg_x

    if b_rates:
        avg_b = sum(b_rates) / len(b_rates)
        min_b, max_b = min(b_rates), max(b_rates)
        print(f"    BN   avg={avg_b*100:+.4f}%/8h  min={min_b*100:+.4f}%  max={max_b*100:+.4f}%  ann={avg_b*3*365*100:+.1f}%  n={len(b_rates)}")
        stats["bn_avg"] = avg_b

    if x_8h_blocks and b_rates:
        sp = avg_x - avg_b
        stats["spread_avg"] = sp
        stats["spread_ann"] = sp * 3 * 365 * 100

        # 일관성 체크: 양수/음수 비율 계산 (최소 샘플 수 기준)
        n = min(len(x_8h_blocks), len(b_rates))
        pos_count = sum(1 for x, b in zip(x_8h_blocks[:n], b_rates[:n]) if x - b > 0)
        consistency = max(pos_count, n - pos_count) / n * 100 if n > 0 else 0
        stats["consistency"] = consistency

        print(f"\n    Spread avg={sp*100:+.4f}%/8h  ann={sp*3*365*100:+.1f}%")
        print(f"    Direction consistency: {consistency:.0f}% (same direction in {max(pos_count, n-pos_count)}/{n} periods)")

        if sp > 0:
            print(f"    -> Strategy: XYZ SHORT + BN LONG")
        else:
            print(f"    -> Strategy: XYZ LONG + BN SHORT")

        if consistency < 60:
            print(f"    ⚠ WARNING: Low consistency (<60%) -- spread direction flips frequently")

    return stats


def save_snapshot(rows):
    ts = datetime.now(timezone.utc).isoformat()
    with open(SNAPSHOT_FILE, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "ts": ts, "ticker": r[0],
                "xyz_8h": r[1], "bn_8h": r[2],
                "spread": r[3], "annual": r[4], "dir": r[5],
                "xyz_oi": r[6], "bn_oi": r[7],
            }) + "\n")
    print(f"\n  Snapshot -> {SNAPSHOT_FILE}")


def save_spread_summary(all_stats: dict):
    ts = datetime.now(timezone.utc).isoformat()
    with open(SPREAD_HISTORY_FILE, "a", encoding="utf-8") as f:
        for ticker, stats in all_stats.items():
            if stats:
                f.write(json.dumps({"ts": ts, "ticker": ticker, **stats}) + "\n")


def main():
    args = sys.argv[1:]
    loop_min = 0
    hist_days = 7
    top_n = 3

    i = 0
    while i < len(args):
        if args[i] == "--loop" and i + 1 < len(args):
            loop_min = int(args[i+1]); i += 2
        elif args[i] == "--history" and i + 1 < len(args):
            hist_days = int(args[i+1]); i += 2
        elif args[i] == "--top" and i + 1 < len(args):
            top_n = int(args[i+1]); i += 2
        else:
            i += 1

    iteration = 0
    while True:
        iteration += 1
        try:
            rows = compare_current()
            save_snapshot(rows)

            actionable = [r for r in rows if r[5].strip() != "-"]
            all_stats = {}

            print(f"\n{'='*75}")
            print(f"  Historical Analysis (TOP {top_n}, {hist_days}d)")
            print(f"{'='*75}")

            for r in actionable[:top_n]:
                ticker = r[0]
                bn_sym = OVERLAP_PAIRS[ticker]
                try:
                    stats = historical_analysis(ticker, bn_sym, days=hist_days)
                    all_stats[ticker] = stats
                except Exception as e:
                    print(f"    {ticker} history error: {e}")
                time.sleep(0.3)

            if all_stats:
                save_spread_summary(all_stats)

        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()

        if loop_min <= 0:
            break

        next_time = datetime.now(timezone.utc).strftime("%H:%M UTC")
        print(f"\n  [{next_time}] Iteration {iteration} done. Next in {loop_min}m... (Ctrl+C to stop)")
        try:
            time.sleep(loop_min * 60)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break

    print("\nDone.")


if __name__ == "__main__":
    main()
