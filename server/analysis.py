"""分析服务层：把原始行情加工成"可决策"的结构化结论。

包含三块：
1. analyze_stock   个股全方位研判（趋势评分 / 主力行为识别 / 资金 / 涨停档案 / 强弱对比）
2. analyze_market  市场总览（指数 / 情绪温度 / 涨跌家数 / 涨停梯队 / 板块资金）
3. analyze_sectors 板块资金与强弱排行
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import datasource as ds
from . import indicators as ind
from .net import parallel_map

LIMIT_RATE = {"main": 9.8, "gem": 19.5, "star": 19.5, "bj": 29.5}


def _limit_rate(code: str, market: str) -> float:
    if market == "bj" or code.startswith(("43", "83", "87", "88", "92")):
        return LIMIT_RATE["bj"]
    if code.startswith(("300", "301")):
        return LIMIT_RATE["gem"]
    if code.startswith(("688", "689")):
        return LIMIT_RATE["star"]
    return LIMIT_RATE["main"]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _pct_change(values: List[float], days: int) -> float:
    if len(values) <= days or values[-1 - days] == 0:
        return 0.0
    return (values[-1] - values[-1 - days]) / values[-1 - days] * 100


# --------------------------------------------------------------------------- #
# 主力行为识别
# --------------------------------------------------------------------------- #
def detect_main_force(
    rows: List[Dict[str, Any]], flows: List[Dict[str, Any]], tech: Dict[str, Any]
) -> Dict[str, Any]:
    """基于量价 + 资金流的程序化主力行为判定（吸筹/洗盘/拉升/出货/震荡）。"""
    if len(rows) < 25:
        return {"phase": "数据不足", "confidence": 0, "signals": [], "summary": "K线样本不足，暂不判定"}

    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    opens = [r["open"] for r in rows]
    vols = [r["volume"] for r in rows]
    latest = tech.get("latest", {})
    close = closes[-1]

    high60 = ind.highest(highs, max(0, len(highs) - 60), len(highs))
    low60 = ind.lowest(lows, max(0, len(lows) - 60), len(lows))
    pos = 0.5 if high60 == low60 else (close - low60) / (high60 - low60)

    vol_last3 = sum(vols[-3:]) / 3 if len(vols) >= 3 else 0
    vol_prev5 = sum(vols[-8:-3]) / 5 if len(vols) >= 8 else (vol_last3 or 1)
    shrink = vol_last3 / vol_prev5 if vol_prev5 else 1.0

    net5 = sum(_num(f.get("net_amount")) for f in flows[:5])
    net3 = sum(_num(f.get("net_amount")) for f in flows[:3])
    net10 = sum(_num(f.get("net_amount")) for f in flows[:10])
    consec = 0
    for f in flows:
        if _num(f.get("net_amount")) > 0:
            consec += 1
        else:
            break
    consec_out = 0
    for f in flows:
        if _num(f.get("net_amount")) < 0:
            consec_out += 1
        else:
            break

    ma5, ma10, ma20, ma60 = (_num(latest.get(k)) for k in ("ma5", "ma10", "ma20", "ma60"))
    hist = _num(latest.get("hist"))
    pct3 = _pct_change(closes, 3)
    pct20 = _pct_change(closes, 20)

    body = abs(close - opens[-1])
    lower_shadow = min(close, opens[-1]) - lows[-1]
    upper_shadow = highs[-1] - max(close, opens[-1])
    avg_turnover = sum(_num(f.get("turnover")) for f in flows[:3]) / 3 if flows else 0
    vol_ratio_today = vols[-1] / (latest.get("vol_ma5") or vols[-1] or 1)

    signals: List[Dict[str, Any]] = []

    def add(name: str, hit: bool, detail: str, weight: int = 1, side: str = "bull") -> None:
        signals.append({"name": name, "hit": bool(hit), "detail": detail, "weight": weight, "side": side})

    add("站上中期均线", close > ma20 and close > ma60 and ma20 > 0,
        "现价 {:.2f}，MA20 {:.2f}，MA60 {:.2f}".format(close, ma20, ma60), 1)
    add("均线多头排列", ma5 > ma10 > ma20 > ma60 > 0,
        "MA5 {:.2f} > MA10 {:.2f} > MA20 {:.2f} > MA60 {:.2f}".format(ma5, ma10, ma20, ma60), 2)
    add("MACD 多头", hist > 0, "MACD 柱 {:.3f}".format(hist), 1)
    add("缩量回调", shrink < 0.9 and pct3 <= 0,
        "近3日均量为前5日的 {:.0%}，近3日涨跌 {:+.2f}%".format(shrink, pct3), 2)
    add("放量突破", vol_ratio_today > 1.4 and close > opens[-1] and close >= high60 * 0.97,
        "量比(对5日均量) {:.2f}，距60日高点 {:.2f}%".format(vol_ratio_today, (close / high60 - 1) * 100 if high60 else 0), 2)
    add("主力资金流入", net5 > 0, "近5日净流入 {:.2f}".format(net5), 2)
    add("资金连续流入", consec >= 3, "连续 {} 日净流入".format(consec), 1)
    add("长下影承接", lower_shadow > body * 1.6 and lower_shadow > 0,
        "下影线长度 {:.2f}".format(lower_shadow), 1)
    add("高换手滞涨", avg_turnover > 3 and abs(pct3) < 2,
        "近3日均换手 {:.2f}%，累计涨跌 {:+.2f}%".format(avg_turnover, pct3), 1, side="bear")
    add("冲高回落", upper_shadow > body * 1.6 and upper_shadow > 0 and close < highs[-1] * 0.98,
        "上影线长度 {:.2f}".format(upper_shadow), 2, side="bear")
    add("跌破中期均线", close < ma20 and ma20 > 0 and close < ma60,
        "现价 {:.2f} 低于 MA20 {:.2f}".format(close, ma20), 2, side="bear")
    add("资金持续流出", net5 < 0 and consec_out >= 3,
        "近5日净流入 {:.2f}，连续 {} 日净流出".format(net5, consec_out), 2, side="bear")
    add("量价背离", pct3 > 2 and net3 < 0,
        "近3日涨 {:+.2f}% 但资金净流出 {:.2f}".format(pct3, net3), 2, side="bear")

    bull = sum(s["weight"] for s in signals if s["side"] == "bull" and s["hit"])
    bear = sum(s["weight"] for s in signals if s["side"] == "bear" and s["hit"])
    total = sum(s["weight"] for s in signals) or 1

    def hit(name: str) -> bool:
        for s in signals:
            if s["name"] == name:
                return s["hit"]
        return False

    if pos >= 0.7 and (hit("高换手滞涨") or hit("冲高回落") or hit("资金持续流出")) and bear >= bull:
        phase, conf = "出货", min(95, 45 + bear * 7)
        summary = "股价处于近60日高位区（分位 {:.0%}），伴随放量滞涨/资金流出，警惕主力派发。".format(pos)
    elif hit("放量突破") and bull >= 6 and pos < 0.92:
        phase, conf = "拉升", min(95, 45 + bull * 6)
        summary = "放量突破叠加均线与资金共振，主力处于拉升阶段，关注量能能否持续。"
    elif hit("缩量回调") and hit("站上中期均线") and net5 > 0:
        phase, conf = "洗盘", min(90, 45 + (bull + 1) * 6)
        summary = "缩量回调但未破中期均线、资金维持净流入，符合洗盘特征。"
    elif bull >= 6 and pos <= 0.6 and (hit("缩量回调") or hit("主力资金流入")):
        phase, conf = "吸筹", min(92, 40 + bull * 6)
        summary = "股价处于区间中低位（分位 {:.0%}）且资金持续流入，疑似主力吸筹。".format(pos)
    elif bear >= 6 and bear - bull >= 3:
        phase, conf = "走弱", min(90, 40 + bear * 6)
        summary = "多项空头信号共振，主力资金离场，中期趋势转弱。"
    elif bull - bear >= 4:
        phase, conf = "多头占优", min(85, 45 + (bull - bear) * 5)
        summary = "多头信号占优但尚未形成明确拉升，主力偏积极，可跟踪量能确认。"
    elif bear - bull >= 4:
        phase, conf = "空头占优", min(85, 45 + (bear - bull) * 5)
        summary = "空头信号占优，资金与均线结构偏弱，宜控制风险。"
    else:
        phase, conf = "震荡待确认", 45 + abs(bull - bear) * 4
        summary = "多空信号存在分歧，主力意图尚不明确，建议等待方向选择。"

    return {
        "phase": phase,
        "confidence": int(min(conf, 95)),
        "bull_score": bull,
        "bear_score": bear,
        "position_60d": round(pos * 100, 1),
        "signals": signals,
        "hit_count": sum(1 for s in signals if s["hit"]),
        "total_signals": len(signals),
        "summary": summary,
        "net_flow_5d": net5,
        "net_flow_10d": net10,
        "consec_inflow": consec,
        "consec_outflow": consec_out,
    }


# --------------------------------------------------------------------------- #
# 个股分析
# --------------------------------------------------------------------------- #
def analyze_stock(query: str) -> Dict[str, Any]:
    code = ds.normalize(query)
    if not code:
        found = ds.search_stock(query)
        if not found:
            return {"error": "未找到该股票，请检查代码或名称", "query": query}
        code = ds.normalize(found[0]["full"])
        if not code:
            return {"error": "未找到该股票，请检查代码或名称", "query": query}

    full = code["full"]
    quote_map, day_rows, week_rows, month_rows, flows, minute, index_map = parallel_map(
        lambda fn: fn(),
        [
            lambda: ds.fetch_quotes([full]),
            lambda: ds.fetch_kline(full, "day", 700),
            lambda: ds.fetch_kline(full, "week", 260),
            lambda: ds.fetch_kline(full, "month", 140),
            lambda: ds.fetch_stock_fundflow(full, 120),
            lambda: ds.fetch_minute(full),
            lambda: ds.fetch_quotes(["sh000001"]),
        ],
        workers=7,
    )

    quote = (quote_map or {}).get(full) or {}
    if not quote:
        return {"error": "未获取到该股票的实时行情（可能已停牌或代码不存在）", "code": code}

    tech = ind.compute_all(day_rows)
    tech_week = ind.compute_all(week_rows)
    main_force = detect_main_force(day_rows, flows, tech)

    warnings = []
    if not day_rows:
        warnings.append("K线数据暂时获取失败（数据源限流或网络异常），趋势、主力行为与涨停档案不可用，请稍后刷新重试")
    if not flows:
        warnings.append("资金流向数据暂时不可用，资金相关结论仅供参考")

    limit_up_history = _limit_up_history(day_rows, code)
    vs_market = _vs_market(day_rows, index_map or {}, full)
    billboard = ds.fetch_billboard(code["code"], 10)

    return {
        "code": code["full"],
        "warnings": warnings,
        "quote": _decorate_quote(quote),
        "kline": {
            "day": _decorate_kline(day_rows, tech, chart_limit=250),
            "week": _decorate_kline(week_rows, tech_week, chart_limit=200),
            "month": _decorate_kline(month_rows, ind.compute_all(month_rows), chart_limit=140),
        },
        "minute": minute,
        "indicators": tech.get("latest", {}),
        "trend": _trend_report(day_rows, tech, main_force),
        "main_force": main_force,
        "fund_flow": _flow_report(flows),
        "limit_up": limit_up_history,
        "vs_market": vs_market,
        "billboard": billboard,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _decorate_quote(q: Dict[str, Any]) -> Dict[str, Any]:
    price = _num(q.get("price"))
    prev = _num(q.get("prev_close"))
    change_pct = _num(q.get("change_pct"))
    if not change_pct and prev:
        change_pct = (price - prev) / prev * 100
    return {
        "name": q.get("name", ""),
        "code": q.get("code", ""),
        "price": price,
        "prev_close": prev,
        "open": _num(q.get("open")),
        "high": _num(q.get("high")),
        "low": _num(q.get("low")),
        "change": _num(q.get("change")),
        "change_pct": change_pct,
        "amount": _num(q.get("amount")),
        "volume": _num(q.get("volume")),
        "turnover": _num(q.get("turnover")),
        "volume_ratio": _num(q.get("volume_ratio")),
        "amplitude": _num(q.get("amplitude")),
        "pe_ttm": _num(q.get("pe_ttm")),
        "pe_dyn": _num(q.get("pe_dyn")),
        "pb": _num(q.get("pb")),
        "total_cap": _num(q.get("total_cap")),
        "float_cap": _num(q.get("float_cap")),
        "limit_up": _num(q.get("limit_up")),
        "limit_down": _num(q.get("limit_down")),
        "high_52w": _num(q.get("high_52w")),
        "low_52w": _num(q.get("low_52w")),
        "time": q.get("time", ""),
    }


def _decorate_kline(rows: List[Dict[str, Any]], tech: Dict[str, Any], chart_limit: int = 0) -> Dict[str, Any]:
    if not rows:
        return {"rows": [], "series": {}}
    series = tech.get("series", {})
    keys = ["ma5", "ma10", "ma20", "ma60", "dif", "dea", "hist", "k", "d", "j",
            "rsi6", "rsi14", "boll_up", "boll_mid", "boll_low"]
    trimmed = rows
    offset = 0
    if chart_limit and len(rows) > chart_limit:
        offset = len(rows) - chart_limit
        trimmed = rows[offset:]
    out_series: Dict[str, List[Optional[float]]] = {}
    for k in keys:
        vals = series.get(k)
        if not vals:
            continue
        vals = vals[offset:]
        out_series[k] = [None if v is None else round(float(v), 4) for v in vals]
    return {"rows": trimmed, "series": out_series}


def _trend_report(rows: List[Dict[str, Any]], tech: Dict[str, Any], main_force: Dict[str, Any]) -> Dict[str, Any]:
    latest = tech.get("latest", {})
    if not rows:
        return {}
    closes = [r["close"] for r in rows]
    ma5, ma10, ma20, ma60 = (_num(latest.get(k)) for k in ("ma5", "ma10", "ma20", "ma60"))
    close = closes[-1]
    hist = _num(latest.get("hist"))
    change_20 = _pct_change(closes, 20)
    change_60 = _pct_change(closes, 60)

    score = 50
    labels: List[str] = []
    if close > ma20 > 0:
        score += 10
        labels.append("站上MA20")
    else:
        score -= 10
    if close > ma60 > 0:
        score += 10
        labels.append("站上MA60")
    else:
        score -= 8
    if ma5 > ma10 > ma20 > 0:
        score += 12
        labels.append("短期多头排列")
    if ma20 > ma60 > 0:
        score += 10
        labels.append("中期多头")
    else:
        score -= 8
        labels.append("中期均线偏空")
    if hist > 0:
        score += 8
        labels.append("MACD金叉")
    else:
        score -= 8
        labels.append("MACD死叉")
    score += min(10, max(-10, change_20))
    score = int(max(5, min(95, score)))

    if score >= 75:
        level, tone = "强势多头", "bull"
    elif score >= 60:
        level, tone = "偏多震荡", "bull"
    elif score >= 45:
        level, tone = "震荡整理", "neutral"
    elif score >= 30:
        level, tone = "偏空震荡", "bear"
    else:
        level, tone = "弱势下行", "bear"

    return {
        "score": score,
        "level": level,
        "tone": tone,
        "labels": labels,
        "change_5d": _pct_change(closes, 5),
        "change_20d": change_20,
        "change_60d": change_60,
        "support": round(ind.lowest([r["low"] for r in rows], max(0, len(rows) - 60), len(rows)), 2),
        "resistance": round(ind.highest([r["high"] for r in rows], max(0, len(rows) - 60), len(rows)), 2),
    }


def _flow_report(flows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not flows:
        return {"today": None, "trend": [], "consec_inflow": 0, "net_5d": 0, "net_10d": 0, "net_20d": 0}
    today = flows[0]
    consec = 0
    for f in flows:
        if _num(f.get("net_amount")) > 0:
            consec += 1
        else:
            break
    return {
        "today": today,
        "trend": list(reversed(flows[:40])),
        "consec_inflow": consec,
        "net_5d": sum(_num(f.get("net_amount")) for f in flows[:5]),
        "net_10d": sum(_num(f.get("net_amount")) for f in flows[:10]),
        "net_20d": sum(_num(f.get("net_amount")) for f in flows[:20]),
    }


def _limit_up_history(rows: List[Dict[str, Any]], code: Dict[str, str], limit: int = 40) -> Dict[str, Any]:
    if len(rows) < 2:
        return {"records": [], "count_250d": 0, "last": None}
    rate = _limit_rate(code["code"], code["market"])
    records: List[Dict[str, Any]] = []
    prev_limit = False
    streak = 0
    for i in range(1, len(rows)):
        prev_close = rows[i - 1]["close"]
        if prev_close <= 0:
            prev_limit = False
            continue
        pct = (rows[i]["close"] - prev_close) / prev_close * 100
        if pct >= rate:
            streak = streak + 1 if prev_limit else 1
            records.append(
                {
                    "date": rows[i]["date"],
                    "change_pct": round(pct, 2),
                    "close": rows[i]["close"],
                    "volume": rows[i]["volume"],
                    "streak": streak,
                }
            )
            prev_limit = True
        else:
            prev_limit = False
    records.reverse()
    today_dt = datetime.now()
    cnt = 0
    for r in records:
        try:
            days = (today_dt - datetime.strptime(r["date"], "%Y-%m-%d")).days
        except ValueError:
            continue
        if days <= 365:
            cnt += 1
        else:
            break
    last = records[0] if records else None
    last_days = None
    if last:
        try:
            last_days = (today_dt - datetime.strptime(last["date"], "%Y-%m-%d")).days
        except ValueError:
            last_days = None
    return {"records": records[:limit], "count_250d": cnt, "last": last, "last_days_ago": last_days,
            "span_days": len(rows)}


def _vs_market(rows: List[Dict[str, Any]], index_map: Dict[str, Any], full: str) -> Dict[str, Any]:
    idx_quote = index_map.get("sh000001") or {}
    idx_rows = ds.fetch_kline("sh000001", "day", 120)
    result: Dict[str, Any] = {"index": {"name": "上证指数", "price": _num(idx_quote.get("price")),
                                       "change_pct": _num(idx_quote.get("change_pct"))}}
    stock_closes = [r["close"] for r in rows]
    idx_closes = [r["close"] for r in idx_rows]
    for window in (5, 20, 60):
        s = _pct_change(stock_closes, window)
        m = _pct_change(idx_closes, window) if idx_closes else 0.0
        result["d{}".format(window)] = {
            "stock": round(s, 2),
            "index": round(m, 2),
            "diff": round(s - m, 2),
            "outperform": s - m > 0,
        }
    return result


# --------------------------------------------------------------------------- #
# 市场总览
# --------------------------------------------------------------------------- #
def _breadth(snapshot: List[Dict[str, Any]]) -> Dict[str, Any]:
    up = down = flat = 0
    limit_up = limit_down = 0
    amount = 0.0
    up5 = down5 = 0
    for item in snapshot:
        pct = _num(item.get("change_pct"))
        amount += _num(item.get("amount"))
        if pct > 0.001:
            up += 1
        elif pct < -0.001:
            down += 1
        else:
            flat += 1
        if pct >= 9.8:
            limit_up += 1
        elif pct <= -9.8:
            limit_down += 1
        if pct >= 5:
            up5 += 1
        elif pct <= -5:
            down5 += 1
    total = up + down + flat
    return {
        "up": up, "down": down, "flat": flat, "total": total,
        "limit_up": limit_up, "limit_down": limit_down,
        "up5": up5, "down5": down5,
        "amount": amount,
        "up_ratio": (up / total * 100) if total else 0,
    }


def _ladder(pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[int, int] = {}
    for item in pool:
        b = int(_num(item.get("board_count"), 1))
        buckets[b] = buckets.get(b, 0) + 1
    max_board = max(buckets.keys()) if buckets else 0
    ladder = [{"board": b, "count": buckets.get(b, 0)} for b in range(1, max(4, max_board) + 1)]
    return {"ladder": ladder, "max_board": max_board, "total": len(pool)}


def _sentiment(breadth: Dict[str, Any], ladder: Dict[str, Any], broken: int) -> Dict[str, Any]:
    """市场情绪温度：0-100，越高越热。"""
    up_ratio = _num(breadth.get("up_ratio"), 50)
    score = 0.0
    score += (up_ratio - 50) * 0.9                       # 涨跌家数贡献 ±45
    score += min(20.0, _num(breadth.get("limit_up")) * 0.6)   # 涨停数量
    score -= min(15.0, _num(breadth.get("limit_down")) * 1.5)  # 跌停数量
    score += min(15.0, _num(ladder.get("max_board")) * 3)      # 连板高度
    seal = _num(ladder.get("total"))
    if seal + broken > 0:
        broken_rate = broken / (seal + broken)
        score -= broken_rate * 25                          # 炸板率惩罚
    temp = int(max(0, min(100, 50 + score)))
    if temp >= 75:
        level, tone = "过热", "bull"
    elif temp >= 60:
        level, tone = "偏暖", "bull"
    elif temp >= 45:
        level, tone = "中性", "neutral"
    elif temp >= 30:
        level, tone = "偏冷", "bear"
    else:
        level, tone = "冰点", "bear"
    return {"temperature": temp, "level": level, "tone": tone}


def analyze_market() -> Dict[str, Any]:
    index_fulls = [c for c, _ in ds.INDEX_LIST]
    quotes, snapshot, up_pool, down_pool, broken_pool, industry_flow, concept_flow, sector_quotes = parallel_map(
        lambda fn: fn(),
        [
            lambda: ds.fetch_quotes(index_fulls),
            lambda: ds.fetch_market_all(),
            lambda: ds.fetch_limit_pool("up"),
            lambda: ds.fetch_limit_pool("down"),
            lambda: ds.fetch_limit_pool("broken"),
            lambda: ds.fetch_sector_fundflow("industry", 40),
            lambda: ds.fetch_sector_fundflow("concept", 40),
            lambda: ds.fetch_sector_quotes(60),
        ],
        workers=8,
    )

    quote_map = quotes or {}
    index_trends = parallel_map(lambda c: ds.fetch_kline(c, "day", 40), index_fulls, workers=5)
    indices = []
    for i, (full, name) in enumerate(ds.INDEX_LIST):
        q = quote_map.get(full) or {}
        rows = index_trends[i] if i < len(index_trends) else []
        indices.append(
            {
                "code": full,
                "name": name,
                "price": _num(q.get("price")),
                "prev_close": _num(q.get("prev_close")),
                "open": _num(q.get("open")),
                "high": _num(q.get("high")),
                "low": _num(q.get("low")),
                "change": _num(q.get("change")),
                "change_pct": _num(q.get("change_pct")),
                "amount": _num(q.get("amount")),
                "trend": [r["close"] for r in (rows or [])],
                "trend_dates": [r["date"] for r in (rows or [])],
                "trend_high": [r["high"] for r in (rows or [])],
                "trend_low": [r["low"] for r in (rows or [])],
            }
        )

    breadth = _breadth(snapshot or [])
    ladder = _ladder(up_pool or [])
    broken = len(broken_pool or [])
    # 涨跌停家数以官方涨停/跌停池为准（比按涨幅阈值统计更准确，兼容 20%/30% 涨跌幅限制）
    if up_pool:
        breadth["limit_up"] = len(up_pool)
    if down_pool:
        breadth["limit_down"] = len(down_pool)
    sentiment = _sentiment(breadth, ladder, broken)
    seal_rate = 0.0
    if ladder["total"] + broken > 0:
        seal_rate = ladder["total"] / (ladder["total"] + broken) * 100

    # 板块资金：按净额排序
    def sort_flow(items: List[Dict[str, Any]], top: int = 15) -> Dict[str, Any]:
        items = sorted(items or [], key=lambda x: -_num(x.get("net_amount")))
        return {"inflow": items[:top], "outflow": list(reversed(items[-top:])) if len(items) > top else []}

    return {
        "indices": indices,
        "breadth": breadth,
        "sentiment": sentiment,
        "limit_up_pool": (up_pool or [])[:40],
        "limit_down_pool": (down_pool or [])[:20],
        "ladder": ladder,
        "broken_count": broken,
        "seal_rate": round(seal_rate, 1),
        "sector_fund": {
            "industry": sort_flow(industry_flow or []),
            "concept": sort_flow(concept_flow or []),
        },
        "sector_rank": sorted(sector_quotes or [], key=lambda x: -_num(x.get("change_pct")))[:20],
        "sector_rank_bottom": sorted(sector_quotes or [], key=lambda x: _num(x.get("change_pct")))[:10],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def analyze_sectors(kind: str = "industry") -> Dict[str, Any]:
    flow = ds.fetch_sector_fundflow(kind, 60)
    quotes = ds.fetch_sector_quotes(80) if kind == "industry" else []
    quote_map = {q["name"]: q for q in quotes}
    merged = []
    for item in flow or []:
        q = quote_map.get(item["name"], {})
        merged.append(
            {
                "name": item["name"],
                "change_pct": _num(item.get("change_pct")),
                "net_amount": _num(item.get("net_amount")),
                "net_ratio": _num(item.get("net_ratio")),
                "change_5d": _num(q.get("change_5d")),
                "change_20d": _num(q.get("change_20d")),
                "leader_name": item.get("leader_name") or q.get("leader_name", ""),
                "leader_change_pct": _num(item.get("leader_change_pct") or q.get("leader_change_pct")),
            }
        )
    merged.sort(key=lambda x: -_num(x.get("net_amount")))
    return {
        "kind": kind,
        "items": merged,
        "inflow": merged[:15],
        "outflow": list(reversed(merged[-15:])) if len(merged) > 15 else [],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------- #
# 供 AI 使用的上下文压缩
# --------------------------------------------------------------------------- #
def build_stock_ai_context(data: Dict[str, Any]) -> Dict[str, Any]:
    q = data.get("quote", {})
    t = data.get("trend", {})
    f = data.get("fund_flow", {})
    mf = data.get("main_force", {})
    ind_latest = data.get("indicators", {})
    return {
        "股票": "{} {}".format(q.get("name", ""), data.get("code", "")),
        "行情": {
            "现价": q.get("price"), "涨跌幅": q.get("change_pct"), "换手率": q.get("turnover"),
            "量比": q.get("volume_ratio"), "成交额(元)": q.get("amount"), "振幅": q.get("amplitude"),
            "总市值(元)": q.get("total_cap"), "市盈率TTM": q.get("pe_ttm"), "市净率": q.get("pb"),
            "今开": q.get("open"), "最高": q.get("high"), "最低": q.get("low"), "昨收": q.get("prev_close"),
        },
        "技术指标": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ind_latest.items()},
        "趋势": t,
        "资金": {
            "今日净流入": (f.get("today") or {}).get("net_amount"),
            "近5日净流入": f.get("net_5d"), "近10日净流入": f.get("net_10d"), "近20日净流入": f.get("net_20d"),
            "连续净流入天数": f.get("consec_inflow"),
        },
        "主力行为": {
            "判定": mf.get("phase"), "置信度": mf.get("confidence"),
            "多空权重": {"多": mf.get("bull_score"), "空": mf.get("bear_score")},
            "命中信号": ["{}：{}".format(s["name"], s["detail"]) for s in mf.get("signals", []) if s.get("hit")],
            "未命中信号": ["{}：{}".format(s["name"], s["detail"]) for s in mf.get("signals", []) if not s.get("hit")],
        },
        "涨停档案": {
            "近一年涨停次数": data.get("limit_up", {}).get("count_250d"),
            "最近涨停": data.get("limit_up", {}).get("last"),
        },
        "相对大盘": data.get("vs_market"),
        "龙虎榜": (data.get("billboard") or [])[:5],
    }


def build_market_ai_context(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "指数": data.get("indices"),
        "涨跌家数": data.get("breadth"),
        "情绪温度": data.get("sentiment"),
        "涨停梯队": data.get("ladder"),
        "炸板数": data.get("broken_count"),
        "封板率": data.get("seal_rate"),
        "行业资金流入TOP": (data.get("sector_fund", {}).get("industry", {}).get("inflow") or [])[:10],
        "概念资金流入TOP": (data.get("sector_fund", {}).get("concept", {}).get("inflow") or [])[:10],
        "行业涨幅榜": (data.get("sector_rank") or [])[:10],
        "龙头股": (data.get("limit_up_pool") or [])[:10],
    }


def build_screener_ai_context(strategy_name: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "选股策略": strategy_name,
        "命中数量": len(items),
        "命中标的": [
            {
                "代码": it.get("code"), "名称": it.get("name"),
                "现价": it.get("price"), "涨跌幅": it.get("change_pct"),
                "成交额(元)": it.get("amount"), "换手率": it.get("turnover"),
                "市盈率": it.get("pe"), "总市值(元)": it.get("total_cap"),
                "匹配说明": it.get("reason"),
            }
            for it in items[:40]
        ],
    }
