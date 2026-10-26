"""选股引擎：内置策略 + 自定义多因子条件。

设计要点
- 候选池取自全市场成交额活跃股（默认 400 只），与"抓活跃股机会"的实战需求一致，
  同时把请求量从 5000+ 降到数百，配合 120s 缓存可反复筛选；
- 每只候选仅拉取一次日 K（90 根），全部指标本地计算，规则可解释；
- 支持进度回调与中途取消，扫描过程对前端可视化。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from . import datasource as ds
from . import indicators as ind
from .net import parallel_map_progress

SCAN_DAYS = 90


def _num(v: Any, d: float = 0.0) -> float:
    try:
        f = float(v)
        return d if f != f else f
    except (TypeError, ValueError):
        return d


def _limit_rate(code: str) -> float:
    if code.startswith(("300", "301", "688", "689")):
        return 19.5
    if code.startswith(("43", "83", "87", "88", "92")):
        return 29.5
    return 9.8


# --------------------------------------------------------------------------- #
# 策略定义
# --------------------------------------------------------------------------- #
def _ctx(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(rows) < 30:
        return None
    tech = ind.compute_all(rows)
    latest = tech.get("latest", {})
    closes = [r["close"] for r in rows]
    return {"rows": rows, "tech": tech, "latest": latest, "closes": closes}


def strat_trend_bull(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    rows, latest, closes = ctx["rows"], ctx["latest"], ctx["closes"]
    ma5 = _num(latest.get("ma5"))
    ema12, ema50 = _num(latest.get("ema12")), _num(latest.get("ema50"))
    hist = _num(latest.get("hist"))
    if ma5 <= 0 or len(closes) < 5:
        return None
    if not all(c > ma5 for c in closes[-3:]):
        return None
    if hist <= 0 or ema12 <= ema50:
        return None
    if _num(meta.get("amount")) < 1e8:
        return None
    return "连续3日站上MA5，MACD柱{:.3f}>0，EMA12>EMA50，成交额{:.2f}亿".format(
        hist, _num(meta.get("amount")) / 1e8)


def strat_first_board_wash(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    rows, latest, closes = ctx["rows"], ctx["latest"], ctx["closes"]
    code = meta.get("code", "")
    rate = _limit_rate(code)
    limit_idx = None
    limit_vol = 0.0
    for i in range(len(rows) - 1, max(0, len(rows) - 30), -1):
        prev_close = rows[i - 1]["close"]
        if prev_close <= 0:
            continue
        pct = (rows[i]["close"] - prev_close) / prev_close * 100
        if pct >= rate:
            limit_idx = i
            limit_vol = rows[i]["volume"]
        elif limit_idx is not None:
            break
    if limit_idx is None or limit_idx < len(rows) - 22:
        return None
    after = rows[limit_idx + 1:]
    if not after:
        return None
    ma20 = _num(latest.get("ma20"))
    close = closes[-1]
    if ma20 <= 0 or close < ma20 * 0.97:
        return None
    v_last5 = sum(r["volume"] for r in rows[-5:]) / 5
    if limit_vol <= 0 or v_last5 > limit_vol * 0.7:
        return None
    days = len(rows) - 1 - limit_idx
    hi_after = max(r["high"] for r in after)
    pull = (hi_after - close) / hi_after * 100 if hi_after else 0
    return "{}日前首板涨停，其后缩量洗盘（近5日均量仅涨停日的{:.0%}），回撤{:.1f}%未破MA20".format(
        days, v_last5 / limit_vol, pull)


def strat_macd_golden(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    rows, latest, closes = ctx["rows"], ctx["latest"], ctx["closes"]
    series = ctx["tech"].get("series", {})
    dif, dea = series.get("dif") or [], series.get("dea") or []
    if len(dif) < 3 or len(dea) < 3:
        return None
    golden = False
    for k in range(-3, 0):
        a0, b0 = dif[k - 1], dea[k - 1]
        a1, b1 = dif[k], dea[k]
        if None in (a0, b0, a1, b1):
            continue
        if a0 <= b0 and a1 > b1:
            golden = True
            break
    ma20 = _num(latest.get("ma20"))
    if not golden or ma20 <= 0 or closes[-1] < ma20:
        return None
    return "近3日MACD金叉（DIF {:.3f} 上穿 DEA {:.3f}），且现价站上MA20".format(
        _num(latest.get("dif")), _num(latest.get("dea")))


def strat_breakout_new_high(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    rows = ctx["rows"]
    closes = ctx["closes"]
    if len(rows) < 61:
        return None
    prev_high = max(r["high"] for r in rows[-61:-1])
    today = rows[-1]
    if prev_high <= 0 or today["close"] < prev_high:
        return None
    if _num(meta.get("change_pct")) < 2.5:
        return None
    v5 = sum(r["volume"] for r in rows[-6:-1]) / 5
    if v5 <= 0 or today["volume"] < v5 * 1.3:
        return None
    return "收盘价突破60日新高，今日涨幅{:+.2f}%，成交量为前5日均量的{:.2f}倍".format(
        _num(meta.get("change_pct")), today["volume"] / v5)


def strat_volume_launch(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    rows = ctx["rows"]
    if len(rows) < 61:
        return None
    highs = [r["high"] for r in rows[-60:]]
    lows = [r["low"] for r in rows[-60:]]
    hi, lo = max(highs), min(lows)
    close = rows[-1]["close"]
    if hi <= lo:
        return None
    pos = (close - lo) / (hi - lo)
    v5 = sum(r["volume"] for r in rows[-6:-1]) / 5
    if pos > 0.4 or v5 <= 0 or rows[-1]["volume"] < v5 * 1.8:
        return None
    if _num(meta.get("change_pct")) < 2.0 or _num(meta.get("turnover")) < 2.5:
        return None
    return "处于60日区间下{:.0%}位置，今日放量至5日均量{:.2f}倍，涨幅{:+.2f}%，换手{:.2f}%".format(
        pos + 0.0, rows[-1]["volume"] / v5, _num(meta.get("change_pct")), _num(meta.get("turnover")))


def strat_strong_pullback(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    rows = ctx["rows"]
    latest = ctx["latest"]
    closes = ctx["closes"]
    if len(closes) < 61:
        return None
    chg60 = (closes[-1] - closes[-61]) / closes[-61] * 100 if closes[-61] else 0
    if chg60 < 15:
        return None
    v3 = sum(r["volume"] for r in rows[-3:]) / 3
    v10 = sum(r["volume"] for r in rows[-13:-3]) / 10
    if v10 <= 0 or v3 > v10 * 0.85:
        return None
    ma20 = _num(latest.get("ma20"))
    if ma20 <= 0 or closes[-1] < ma20:
        return None
    return "60日累计涨幅{:+.1f}%，近3日缩量至10日均量的{:.0%}回调，仍站稳MA20".format(
        chg60, v3 / v10)


def strat_limit_up_pullback(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    rows = ctx["rows"]
    code = meta.get("code", "")
    rate = _limit_rate(code)
    closes = ctx["closes"]
    limit_idx = None
    for i in range(len(rows) - 1, max(0, len(rows) - 12), -1):
        prev_close = rows[i - 1]["close"]
        if prev_close <= 0:
            continue
        pct = (rows[i]["close"] - prev_close) / prev_close * 100
        if pct >= rate:
            limit_idx = i
            break
    if limit_idx is None or limit_idx >= len(rows) - 1:
        return None
    latest = ctx["latest"]
    ma10, ma20 = _num(latest.get("ma10")), _num(latest.get("ma20"))
    close = closes[-1]
    if ma10 <= 0 or close < ma10 * 0.96:
        return None
    if _num(meta.get("change_pct")) < 0:
        return None
    return "{}日前涨停，回调至MA10({:.2f})附近获支撑，今日止跌回升".format(
        len(rows) - 1 - limit_idx, ma10)


# --------------------------------------------------------------------------- #
# 量化 / 机构常用方法
# --------------------------------------------------------------------------- #
def _seg_amp(seg: List[Dict[str, Any]]) -> float:
    """一段区间的振幅（(最高-最低)/最低）。"""
    if not seg:
        return 0.0
    hi = max(r["high"] for r in seg)
    lo = min(r["low"] for r in seg)
    return (hi - lo) / lo if lo > 0 else 0.0


def strat_vcp_squeeze(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """VCP 波动收缩：三段振幅逐级收窄 + 量能萎缩 + 贴近高点。"""
    rows = ctx["rows"]
    if len(rows) < 60:
        return None
    a1 = _seg_amp(rows[-60:-40])
    a2 = _seg_amp(rows[-40:-20])
    a3 = _seg_amp(rows[-20:])
    if not (a3 < a2 and a2 < a1 * 1.02):
        return None
    hi60 = max(r["high"] for r in rows[-60:])
    close = ctx["closes"][-1]
    gap = (hi60 - close) / hi60 * 100 if hi60 > 0 else 99
    if gap > 15:
        return None
    v5 = sum(r["volume"] for r in rows[-5:]) / 5
    v20 = sum(r["volume"] for r in rows[-25:-5]) / 20
    if v20 <= 0 or v5 > v20 * 1.05:
        return None
    return "三段振幅 {:.1%}→{:.1%}→{:.1%} 逐级收缩，距60日高点仅{:.1f}%，近5日量能为前20日的{:.0%}".format(
        a1, a2, a3, gap, v5 / v20)


def strat_ma_converge(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """均线粘合待突破：四条均线极差很小 + 站上MA20 + 量能或涨幅抬头。"""
    latest = ctx["latest"]
    close = ctx["closes"][-1]
    mas = [_num(latest.get(k)) for k in ("ma5", "ma10", "ma20", "ma60")]
    if any(m <= 0 for m in mas) or close <= 0:
        return None
    spread = (max(mas) - min(mas)) / close * 100
    if spread > 3.5:
        return None
    if close < mas[2]:
        return None
    vr = _num(meta.get("volume_ratio"))
    pct = _num(meta.get("change_pct"))
    if vr < 1.15 and pct < 1.0:
        return None
    return "MA5/10/20/60 极差仅{:.2f}%（均线粘合），现价站上MA20，量比{:.2f}、涨幅{:+.2f}%".format(
        spread, vr, pct)


def strat_low_vol_momentum(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """低波动动量：中期有涨幅但日常波动小，机构偏好的稳健趋势票。"""
    rows, closes = ctx["rows"], ctx["closes"]
    if len(closes) < 61:
        return None
    chg60 = (closes[-1] - closes[-61]) / closes[-61] * 100 if closes[-61] else 0
    if not (8 <= chg60 <= 80):
        return None
    rets = []
    for i in range(len(closes) - 40, len(closes)):
        if i <= 0 or closes[i - 1] <= 0:
            continue
        rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(rets) < 20:
        return None
    mean = sum(rets) / len(rets)
    vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 * 100
    if vol > 3.2:
        return None
    atr = _num(ctx["latest"].get("atr14"))
    atr_pct = atr / closes[-1] * 100 if closes[-1] > 0 else 99
    if atr_pct > 5:
        return None
    ma20 = _num(ctx["latest"].get("ma20"))
    if ma20 <= 0 or closes[-1] < ma20:
        return None
    return "60日涨{:+.1f}% 且日波动率仅{:.2f}%（ATR占价{:.2f}%），低波动趋势票，站稳MA20".format(
        chg60, vol, atr_pct)


def strat_rsi_reversal(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """超跌反转：RSI6 超卖 + 今日收阳 + 已有明显回撤。"""
    rows = ctx["rows"]
    rsi6 = _num(ctx["latest"].get("rsi6"))
    if rsi6 <= 0 or rsi6 > 32:
        return None
    today = rows[-1]
    if today["close"] <= today["open"]:
        return None
    pct = _num(meta.get("change_pct"))
    if pct <= 0:
        return None
    seg = rows[-60:] if len(rows) >= 60 else rows
    hi = max(r["high"] for r in seg)
    if hi <= 0:
        return None
    drawdown = (hi - today["close"]) / hi * 100
    if drawdown < 12:
        return None
    return "RSI6 仅 {:.1f} 处超卖区，今日收阳{:+.2f}%，距60日高点已回撤{:.1f}%".format(rsi6, pct, drawdown)


def strat_boll_support(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """布林下轨支撑：触及下轨 + 收阳 + 缩量。"""
    rows = ctx["rows"]
    low = _num(ctx["latest"].get("boll_low"))
    close = ctx["closes"][-1]
    if low <= 0 or close > low * 1.03:
        return None
    today = rows[-1]
    if today["close"] <= today["open"]:
        return None
    v3 = sum(r["volume"] for r in rows[-3:]) / 3
    v10 = sum(r["volume"] for r in rows[-13:-3]) / 10
    if v10 <= 0 or v3 > v10 * 1.15:
        return None
    return "现价 {:.2f} 贴近布林下轨 {:.2f}，今日收阳，近3日缩量至10日均量的{:.0%}".format(
        close, low, v3 / v10)


def strat_gap_up_hold(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """跳空不回补：近5日出现向上跳空且缺口守住。"""
    rows = ctx["rows"]
    if len(rows) < 10:
        return None
    gap_idx = None
    for i in range(len(rows) - 1, len(rows) - 6, -1):
        if i <= 0:
            break
        if rows[i]["low"] > rows[i - 1]["high"] * 1.005:
            gap_idx = i
            break
    if gap_idx is None:
        return None
    gap_level = rows[gap_idx - 1]["high"]
    after = rows[gap_idx:]
    if min(r["low"] for r in after) < gap_level:
        return None
    pct = _num(meta.get("change_pct"))
    if pct < 0.5:
        return None
    days = len(rows) - 1 - gap_idx
    when = "今日" if days == 0 else "{}日前".format(days)
    return "{}向上跳空（缺口上沿 {:.2f}）未被回补，今日再涨{:+.2f}%".format(when, gap_level, pct)


def strat_volume_dry_up(ctx: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """地量见底：低位地量后首次放量收阳。"""
    rows = ctx["rows"]
    if len(rows) < 60:
        return None
    v5 = sum(r["volume"] for r in rows[-5:]) / 5
    v60 = sum(r["volume"] for r in rows[-60:]) / 60
    if v60 <= 0 or v5 > v60 * 0.62:
        return None
    hi = max(r["high"] for r in rows[-60:])
    lo = min(r["low"] for r in rows[-60:])
    if hi <= lo:
        return None
    close = ctx["closes"][-1]
    pos = (close - lo) / (hi - lo)
    if pos > 0.40:
        return None
    today = rows[-1]
    if today["close"] <= today["open"]:
        return None
    v_prev5 = sum(r["volume"] for r in rows[-6:-1]) / 5
    if v_prev5 <= 0 or today["volume"] < v_prev5 * 1.2:
        return None
    return "近5日均量仅为60日均量的{:.0%}（地量），处于60日区间下{:.0f}%，今日放量{:.1f}倍收阳".format(
        v5 / v60, pos * 100, today["volume"] / v_prev5)


STRATEGIES: List[Dict[str, Any]] = [
    {"id": "trend_bull", "name": "趋势多头", "desc": "均线多头 + MACD 走强 + 放量",
     "category": "trend",
     "basis": "趋势跟踪：中期均线向上时顺势持有，是公募与量化 CTA 最基础的多头暴露方式",
     "rules": ["最近连续3个交易日收盘价 > MA5", "MACD柱 > 0（DIF 位于 DEA 上方）",
               "EMA12 > EMA50", "当日成交额 > 1亿元"],
     "fn": strat_trend_bull},
    {"id": "first_board_wash", "name": "首板洗盘", "desc": "首次涨停后缩量洗盘，疑似临近拉升",
     "category": "volume",
     "basis": "涨停后缩量回调说明抛压有限，是游资与短线资金常见的二次启动形态",
     "rules": ["近1个月内出现涨停且为首次", "涨停后缩量回调（近5日均量 < 涨停日70%）",
               "回调不破 MA20（现价 ≥ MA20×0.97）"],
     "fn": strat_first_board_wash},
    {"id": "macd_golden", "name": "MACD金叉", "desc": "近3日金叉 + 站上MA20",
     "category": "trend",
     "basis": "MACD 金叉是最广泛使用的趋势转折确认信号，配合 MA20 过滤假信号",
     "rules": ["最近3个交易日内 DIF 上穿 DEA", "现价站上 MA20"],
     "fn": strat_macd_golden},
    {"id": "breakout_new_high", "name": "突破新高", "desc": "突破60日新高 + 放量",
     "category": "trend",
     "basis": "欧奈尔 CANSLIM 的 N（New High）：创新高且量能配合是主升浪起点",
     "rules": ["收盘价 ≥ 前60日最高价", "当日涨幅 ≥ 2.5%", "成交量 ≥ 前5日均量×1.3"],
     "fn": strat_breakout_new_high},
    {"id": "volume_launch", "name": "低位放量", "desc": "区间低位放量启动",
     "category": "volume",
     "basis": "低位放量通常对应资金建仓，量在价先，是底部反转的早期信号",
     "rules": ["股价处于60日区间下40%分位", "成交量 ≥ 前5日均量×1.8",
               "当日涨幅 ≥ 2%", "换手率 ≥ 2.5%"],
     "fn": strat_volume_launch},
    {"id": "strong_pullback", "name": "强势回踩", "desc": "趋势股缩量回踩MA20",
     "category": "trend",
     "basis": "强势股缩量回踩关键均线是低风险介入点，趋势未破则回踩即机会",
     "rules": ["60日累计涨幅 ≥ 15%", "近3日均量 < 前10日均量×0.85", "收盘价 ≥ MA20"],
     "fn": strat_strong_pullback},
    {"id": "limit_up_pullback", "name": "涨停回马枪", "desc": "涨停后回踩均线止跌",
     "category": "reversal",
     "basis": "涨停后的首次回踩若不破MA10，往往是资金二次进攻的观察窗口",
     "rules": ["近10个交易日内出现过涨停", "现价 ≥ MA10×0.96", "当日红盘"],
     "fn": strat_limit_up_pullback},

    # ---------- 量化 / 机构常用方法 ----------
    {"id": "vcp_squeeze", "name": "VCP 波动收缩", "desc": "振幅逐级收窄 + 量能萎缩 + 贴近高点",
     "category": "quant",
     "basis": "Mark Minervini 的 VCP（Volatility Contraction Pattern）：波动与量能逐级收缩，是机构成长股买点的经典形态",
     "rules": ["近60日分三段，振幅逐段收窄（后一段小于前一段）", "距60日高点 ≤ 15%",
               "近5日均量 ≤ 前20日均量×1.05"],
     "fn": strat_vcp_squeeze},
    {"id": "ma_converge", "name": "均线粘合待突破", "desc": "四条均线收敛 + 量能抬头",
     "category": "quant",
     "basis": "均线粘合代表多空成本趋于一致，是方向选择前的蓄势状态，A股量化中常作为突破前置因子",
     "rules": ["MA5/10/20/60 极差 ≤ 现价3.5%", "现价站上 MA20",
               "量比 ≥ 1.15 或 涨幅 ≥ 1%"],
     "fn": strat_ma_converge},
    {"id": "low_vol_momentum", "name": "低波动动量", "desc": "中期上涨 + 日波动率低",
     "category": "quant",
     "basis": "低波动异象：长期看低波动股票风险调整后收益更高，是 Smart Beta 与机构底仓的主流因子",
     "rules": ["60日涨幅 8%~80%", "近40日日收益率标准差 ≤ 3.2%",
               "ATR14 占股价 ≤ 5%", "现价站上 MA20"],
     "fn": strat_low_vol_momentum},
    {"id": "rsi_reversal", "name": "RSI 超跌反转", "desc": "RSI6 超卖 + 收阳 + 已回撤",
     "category": "reversal",
     "basis": "均值回归：短期超卖后出现阳线，是量化反转因子的典型触发条件",
     "rules": ["RSI6 ≤ 32（超卖）", "当日收阳且上涨", "距60日高点回撤 ≥ 12%"],
     "fn": strat_rsi_reversal},
    {"id": "boll_support", "name": "布林下轨支撑", "desc": "触及下轨 + 收阳 + 缩量",
     "category": "reversal",
     "basis": "布林下轨是统计意义上的低估区，缩量触及后收阳说明抛压衰竭",
     "rules": ["现价 ≤ 布林下轨×1.03", "当日收阳", "近3日均量 ≤ 前10日均量×1.15"],
     "fn": strat_boll_support},
    {"id": "gap_up_hold", "name": "跳空不回补", "desc": "向上跳空且缺口守住",
     "category": "volume",
     "basis": "向上跳空且不回补代表资金愿意在更高成本承接，是强势延续信号",
     "rules": ["近5日出现向上跳空（当日最低 > 前日最高×1.005）", "缺口未被回补", "当日涨幅 ≥ 0.5%"],
     "fn": strat_gap_up_hold},
    {"id": "volume_dry_up", "name": "地量见底", "desc": "低位地量后首次放量收阳",
     "category": "volume",
     "basis": "地量见地价：成交极度萎缩代表浮筹出清，随后放量收阳是资金回流的第一信号",
     "rules": ["近5日均量 ≤ 60日均量×0.62", "股价处于60日区间下40%",
               "当日量能 ≥ 前5日均量×1.2 且收阳"],
     "fn": strat_volume_dry_up},
    {"id": "multi_factor", "name": "多因子综合评分", "desc": "动量+趋势+量能+低波+位置 五维打分排名",
     "category": "quant",
     "basis": "机构主流的横截面多因子选股：不做硬阈值筛选，而是对所有候选按因子打分后取排名靠前者，避免单一条件过拟合",
     "rules": ["动量30%：近20日涨幅横截面排名", "趋势25%：站上均线数量 + 均线排列",
               "量能20%：量比排名", "低波15%：40日收益波动率（越低越好）",
               "位置10%：距60日高点 5% 附近最优"],
     "fn": None},
]

_STRAT_MAP = {s["id"]: s for s in STRATEGIES}


# --------------------------------------------------------------------------- #
# 自定义条件
# --------------------------------------------------------------------------- #
DEFAULT_CUSTOM: Dict[str, Any] = {
    "change_pct_min": None, "change_pct_max": None,
    "turnover_min": None, "turnover_max": None,
    "volume_ratio_min": None,
    "cap_min": None, "cap_max": None,          # 总市值，单位：亿元
    "pe_min": None, "pe_max": None,
    "amount_min": None,                         # 成交额，单位：亿元
    "price_above_ma": [],                       # 子集: ma5/ma20/ma60
    "macd_bull": False,
    "ma_align": False,
    "change_60d_min": None,
    "from_high_max": None,                      # 距60日高点百分比上限
}


def _eval_custom(ctx: Dict[str, Any], meta: Dict[str, Any], cond: Dict[str, Any]) -> Optional[str]:
    latest, rows, closes = ctx["latest"], ctx["rows"], ctx["closes"]
    reasons: List[str] = []

    def rng(key, value, label, scale=1.0):
        lo, hi = cond.get(key + "_min"), cond.get(key + "_max")
        if lo is None and hi is None:
            return True
        v = _num(value) * scale
        if lo is not None and v < _num(lo):
            return False
        if hi is not None and v > _num(hi):
            return False
        reasons.append("{} {:.2f}".format(label, v))
        return True

    q = meta
    if not rng("change_pct", q.get("change_pct"), "涨跌幅%"):
        return None
    if not rng("turnover", q.get("turnover"), "换手率%"):
        return None
    if not rng("volume_ratio", q.get("volume_ratio"), "量比"):
        return None
    if not rng("cap", q.get("total_cap"), "总市值(亿)", 1e-8):
        return None
    if not rng("pe", q.get("pe"), "市盈率"):
        return None
    if not rng("amount", q.get("amount"), "成交额(亿)", 1e-8):
        return None

    for ma_key in cond.get("price_above_ma") or []:
        ma = _num(latest.get(ma_key))
        if ma <= 0 or closes[-1] < ma:
            return None
        reasons.append("站上{}".format(ma_key.upper()))

    if cond.get("macd_bull"):
        if _num(latest.get("hist")) <= 0:
            return None
        reasons.append("MACD柱>0")

    if cond.get("ma_align"):
        ma5, ma10, ma20, ma60 = (_num(latest.get(k)) for k in ("ma5", "ma10", "ma20", "ma60"))
        if not (ma5 > ma10 > ma20 > ma60 > 0):
            return None
        reasons.append("均线多头排列")

    if cond.get("change_60d_min") is not None:
        if len(closes) < 61:
            return None
        chg = (closes[-1] - closes[-61]) / closes[-61] * 100 if closes[-61] else 0
        if chg < _num(cond["change_60d_min"]):
            return None
        reasons.append("60日涨幅{:+.1f}%".format(chg))

    if cond.get("from_high_max") is not None:
        hi = max(r["high"] for r in rows[-60:])
        if hi <= 0:
            return None
        gap = (hi - closes[-1]) / hi * 100
        if gap > _num(cond["from_high_max"]):
            return None
        reasons.append("距60日高点{:.1f}%".format(gap))

    return "；".join(reasons) if reasons else "满足全部自定义条件"


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def list_strategies() -> Dict[str, Any]:
    return {
        "strategies": [
            {"id": s["id"], "name": s["name"], "desc": s["desc"],
             "category": s.get("category", "trend"), "basis": s.get("basis", ""),
             "rules": s["rules"]}
            for s in STRATEGIES
        ],
        "categories": [
            {"id": "trend", "name": "趋势跟踪"},
            {"id": "volume", "name": "量价形态"},
            {"id": "reversal", "name": "反转回归"},
            {"id": "quant", "name": "量化 / 机构"},
        ],
        "custom_default": DEFAULT_CUSTOM,
        "pool_note": "候选池为全市场成交额活跃股，扫描深度可调",
    }


def _load_quote_meta(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """补齐量比等实时字段（板块列表接口不含量比）。"""
    fulls = [p["full"] for p in pool]
    quotes = ds.fetch_quotes(fulls)
    for p in pool:
        q = quotes.get(p["full"]) or {}
        p["volume_ratio"] = _num(q.get("volume_ratio"))
        if q.get("amount"):
            p["amount"] = _num(q.get("amount"))
        if q.get("turnover"):
            p["turnover"] = _num(q.get("turnover"))
        if q.get("pe_ttm"):
            p["pe"] = _num(q.get("pe_ttm"))
        if q.get("price"):
            p["price"] = _num(q.get("price"))
        if q.get("change_pct"):
            p["change_pct"] = _num(q.get("change_pct"))
        p["code"] = p.get("code") or p["full"][2:]
    return pool


def _pct_rank(values: List[float], higher_better: bool = True) -> List[float]:
    """横截面百分位排名，输出 0~100。"""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    for pos, i in enumerate(order):
        ratio = pos / (n - 1) if n > 1 else 0.5
        out[i] = ratio * 100 if higher_better else (1 - ratio) * 100
    return out


MF_DAYS = 120
MF_TOP = 40


def _run_multi_factor(
    pool: List[Dict[str, Any]],
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """横截面多因子打分：先采集全部候选的因子值，再统一标准化排名。"""
    now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def task(item: Dict[str, Any]):
        if should_stop and should_stop():
            return None
        rows = ds.fetch_kline(item["full"], "day", MF_DAYS)
        if len(rows) < 61:
            return None
        tech = ind.compute_all(rows)
        latest = tech.get("latest", {})
        closes = [r["close"] for r in rows]
        if not closes or not closes[-1]:
            return None
        base = closes[-21] if len(closes) > 21 else closes[0]
        mom20 = (closes[-1] - base) / base * 100 if base else 0.0

        ma_vals = {k: _num(latest.get(k)) for k in ("ma5", "ma10", "ma20", "ma60")}
        above = sum(1 for v in ma_vals.values() if v > 0 and closes[-1] > v)
        align = 1 if (ma_vals["ma5"] > ma_vals["ma10"] > ma_vals["ma20"] > 0) else 0
        below_ma20 = 1 if (ma_vals["ma20"] > 0 and closes[-1] < ma_vals["ma20"]) else 0
        trend_raw = above * 10 + align * 8 - below_ma20 * 6

        rets = []
        for i in range(max(1, len(closes) - 40), len(closes)):
            if closes[i - 1] > 0:
                rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
        if len(rets) >= 20:
            mean = sum(rets) / len(rets)
            vol = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5 * 100
        else:
            vol = 99.0

        hi60 = max(r["high"] for r in rows[-60:])
        gap = (hi60 - closes[-1]) / hi60 * 100 if hi60 > 0 else 99.0
        return {
            "item": item, "mom20": mom20, "trend_raw": trend_raw,
            "vr": _num(item.get("volume_ratio")), "vol": vol, "gap": gap,
        }

    results = parallel_map_progress(task, pool, workers=5, on_progress=on_progress, should_stop=should_stop)
    records = [r for r in results if r]
    if not records:
        return {"strategy": "multi_factor", "strategy_name": "多因子综合评分",
                "pool_size": len(pool), "valid_count": 0, "count": 0, "items": [],
                "stopped": bool(should_stop and should_stop()), "updated_at": now}

    m_r = _pct_rank([r["mom20"] for r in records])
    t_r = _pct_rank([r["trend_raw"] for r in records])
    v_r = _pct_rank([r["vr"] for r in records])
    l_r = _pct_rank([r["vol"] for r in records], higher_better=False)
    p_r = _pct_rank([-abs(r["gap"] - 5) for r in records])

    for i, r in enumerate(records):
        r["score"] = round(0.30 * m_r[i] + 0.25 * t_r[i] + 0.20 * v_r[i]
                           + 0.15 * l_r[i] + 0.10 * p_r[i], 1)
        r["dims"] = (m_r[i], t_r[i], v_r[i], l_r[i], p_r[i])

    records.sort(key=lambda x: -x["score"])
    items = []
    for r in records[:MF_TOP]:
        it = r["item"]
        d = r["dims"]
        items.append({
            "code": it.get("code"),
            "full": it.get("full"),
            "name": it.get("name"),
            "price": round(_num(it.get("price")), 2),
            "change_pct": round(_num(it.get("change_pct")), 2),
            "amount": _num(it.get("amount")),
            "turnover": round(_num(it.get("turnover")), 2),
            "volume_ratio": round(_num(it.get("volume_ratio")), 2),
            "pe": round(_num(it.get("pe")), 2),
            "total_cap": _num(it.get("total_cap")),
            "score": r["score"],
            "reason": "综合分 {:.1f}｜动量 {:.0f} · 趋势 {:.0f} · 量能 {:.0f} · 低波 {:.0f} · 位置 {:.0f}｜20日{:+.1f}% 距高点{:.1f}% 波动率{:.2f}%".format(
                r["score"], d[0], d[1], d[2], d[3], d[4], r["mom20"], r["gap"], r["vol"]),
        })

    return {
        "strategy": "multi_factor",
        "strategy_name": "多因子综合评分",
        "pool_size": len(pool),
        "valid_count": len(records),
        "count": len(items),
        "items": items,
        "stopped": bool(should_stop and should_stop()),
        "updated_at": now,
    }


def run_screener(
    strategy: str = "trend_bull",
    custom: Optional[Dict[str, Any]] = None,
    pool_size: int = 400,
    max_workers: int = 5,
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    if strategy == "custom":
        cond = dict(DEFAULT_CUSTOM)
        cond.update(custom or {})
        evaluator = lambda ctx, meta: _eval_custom(ctx, meta, cond)  # noqa: E731
        name = "自定义条件"
    elif strategy == "multi_factor":
        pool = ds.fetch_market_top(max(50, min(pool_size, 1200)), sort="amount")
        if not pool:
            return {"error": "候选池获取失败（上游行情接口波动），请稍后重试", "strategy": strategy}
        return _run_multi_factor(_load_quote_meta(pool), on_progress, should_stop)
    else:
        spec = _STRAT_MAP.get(strategy)
        if not spec:
            return {"error": "未知选股策略: " + str(strategy)}
        evaluator = spec["fn"]
        name = spec["name"]

    pool = ds.fetch_market_top(max(50, min(pool_size, 1200)), sort="amount")
    if not pool:
        return {"error": "候选池获取失败（上游行情接口波动），请稍后重试", "strategy": strategy}
    pool = _load_quote_meta(pool)

    valid = [0]
    valid_lock = __import__("threading").Lock()

    def task(item: Dict[str, Any]):
        if should_stop and should_stop():
            return None
        rows = ds.fetch_kline(item["full"], "day", SCAN_DAYS)
        ctx = _ctx(rows)
        if not ctx:
            return None
        with valid_lock:
            valid[0] += 1
        try:
            reason = evaluator(ctx, item)
        except Exception:  # noqa: BLE001 - 单只异常不影响整体
            return None
        if not reason:
            return None
        return {
            "code": item.get("code"),
            "full": item.get("full"),
            "name": item.get("name"),
            "price": round(_num(item.get("price")), 2),
            "change_pct": round(_num(item.get("change_pct")), 2),
            "amount": _num(item.get("amount")),
            "turnover": round(_num(item.get("turnover")), 2),
            "volume_ratio": round(_num(item.get("volume_ratio")), 2),
            "pe": round(_num(item.get("pe")), 2),
            "total_cap": _num(item.get("total_cap")),
            "reason": reason,
        }

    results = parallel_map_progress(task, pool, workers=max_workers,
                                    on_progress=on_progress, should_stop=should_stop)
    items = [r for r in results if r]
    items.sort(key=lambda x: -_num(x.get("amount")))
    return {
        "strategy": strategy,
        "strategy_name": name,
        "pool_size": len(pool),
        "valid_count": valid[0],
        "count": len(items),
        "items": items,
        "stopped": bool(should_stop and should_stop()),
        "updated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
