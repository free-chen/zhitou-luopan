"""技术指标计算（纯函数，输入价格序列，输出同长度序列或标量）。

全部采用国内行情软件通行口径：
- MA/SMA 为简单移动平均；
- EMA 为指数移动平均，初值取首个有效值；
- MACD 参数 (12, 26, 9)，柱 = (DIF - DEA) * 2；
- KDJ 参数 (9, 3, 3)，RSV 采用 9 日区间；
- RSI 采用 Wilder 平滑。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if not values or period <= 0:
        return out
    k = 2.0 / (period + 1)
    prev = values[0]
    out[0] = prev
    for i in range(1, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, List[Optional[float]]]:
    if not closes:
        return {"dif": [], "dea": [], "hist": []}
    ef = ema(closes, fast)
    es = ema(closes, slow)
    dif = [(a or 0) - (b or 0) for a, b in zip(ef, es)]
    dea = ema(dif, signal)
    hist = [((d or 0) - (s or 0)) * 2 for d, s in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


def kdj(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 9
) -> Dict[str, List[Optional[float]]]:
    length = len(closes)
    k_list: List[Optional[float]] = [None] * length
    d_list: List[Optional[float]] = [None] * length
    j_list: List[Optional[float]] = [None] * length
    k = d = 50.0
    for i in range(length):
        lo = min(lows[max(0, i - n + 1) : i + 1])
        hi = max(highs[max(0, i - n + 1) : i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100
        k = (2.0 / 3) * k + (1.0 / 3) * rsv
        d = (2.0 / 3) * d + (1.0 / 3) * k
        k_list[i], d_list[i], j_list[i] = k, d, 3 * k - 2 * d
    return {"k": k_list, "d": d_list, "j": j_list}


def rsi(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gain += max(diff, 0)
        loss += max(-diff, 0)
    avg_gain, avg_loss = gain / period, loss / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def boll(closes: Sequence[float], period: int = 20, k: float = 2.0) -> Dict[str, List[Optional[float]]]:
    mid = sma(closes, period)
    upper: List[Optional[float]] = [None] * len(closes)
    lower: List[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        if i < period - 1 or mid[i] is None:
            continue
        window = closes[i - period + 1 : i + 1]
        mean = mid[i] or 0
        var = sum((x - mean) ** 2 for x in window) / period
        std = var ** 0.5
        upper[i] = mean + k * std
        lower[i] = mean - k * std
    return {"mid": mid, "upper": upper, "lower": lower}


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    trs = [0.0]
    for i in range(1, len(closes)):
        trs.append(
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        )
    return ema(trs, period)


def highest(values: Sequence[float], start: int, end: int) -> float:
    window = values[start:end]
    return max(window) if window else 0.0


def lowest(values: Sequence[float], start: int, end: int) -> float:
    window = values[start:end]
    return min(window) if window else 0.0


def last_valid(series: Sequence[Optional[float]]) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return float(v)
    return None


def cross_up(a: Sequence[Optional[float]], b: Sequence[Optional[float]], idx: int = -1) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    try:
        return (a[idx - 1] is not None and b[idx - 1] is not None
                and a[idx] is not None and b[idx] is not None
                and a[idx - 1] <= b[idx - 1] and a[idx] > b[idx])
    except IndexError:
        return False


def cross_down(a: Sequence[Optional[float]], b: Sequence[Optional[float]], idx: int = -1) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    try:
        return (a[idx - 1] is not None and b[idx - 1] is not None
                and a[idx] is not None and b[idx] is not None
                and a[idx - 1] >= b[idx - 1] and a[idx] < b[idx])
    except IndexError:
        return False


def compute_all(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """一次算出 K 线所需的全部指标序列并返回最新快照。"""
    if not rows:
        return {}
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    vols = [r["volume"] for r in rows]
    m = macd(closes)
    k = kdj(highs, lows, closes)
    b = boll(closes)
    return {
        "series": {
            "ma5": sma(closes, 5),
            "ma10": sma(closes, 10),
            "ma20": sma(closes, 20),
            "ma60": sma(closes, 60),
            "ema12": ema(closes, 12),
            "ema50": ema(closes, 50),
            "dif": m["dif"],
            "dea": m["dea"],
            "hist": m["hist"],
            "k": k["k"],
            "d": k["d"],
            "j": k["j"],
            "rsi6": rsi(closes, 6),
            "rsi14": rsi(closes, 14),
            "boll_mid": b["mid"],
            "boll_up": b["upper"],
            "boll_low": b["lower"],
            "vol_ma5": sma(vols, 5),
            "vol_ma10": sma(vols, 10),
        },
        "latest": {
            "ma5": last_valid(sma(closes, 5)),
            "ma10": last_valid(sma(closes, 10)),
            "ma20": last_valid(sma(closes, 20)),
            "ma60": last_valid(sma(closes, 60)),
            "ema12": last_valid(ema(closes, 12)),
            "ema50": last_valid(ema(closes, 50)),
            "dif": last_valid(m["dif"]),
            "dea": last_valid(m["dea"]),
            "hist": last_valid(m["hist"]),
            "k": last_valid(k["k"]),
            "d": last_valid(k["d"]),
            "j": last_valid(k["j"]),
            "rsi6": last_valid(rsi(closes, 6)),
            "rsi14": last_valid(rsi(closes, 14)),
            "boll_up": last_valid(b["upper"]),
            "boll_mid": last_valid(b["mid"]),
            "boll_low": last_valid(b["lower"]),
            "atr14": last_valid(atr(highs, lows, closes, 14)),
            "vol_ma5": last_valid(sma(vols, 5)),
            "vol_ma10": last_valid(sma(vols, 10)),
            "close": closes[-1],
        },
    }
