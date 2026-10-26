"""数据源适配层。

多源冗余设计（东方财富 push2* 系列接口对非浏览器客户端存在风控，故主体改用腾讯/新浪，
东方财富仅用于其独有的涨停池与龙虎榜数据）：

    腾讯  qt.gtimg.cn / web.ifzq.gtimg.cn / proxy.finance.qq.com / smartbox.gtimg.cn
    新浪  vip.stock.finance.sina.com.cn
    东财  push2ex.eastmoney.com（涨停池）/ datacenter-web.eastmoney.com（龙虎榜）

所有函数失败时返回空值，由上层做降级，不向上抛异常。
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config
from .net import cache, cached, http_get, http_get_json, note_upstream, parallel_map, throttle

REF_QQ = "https://gu.qq.com/"
REF_SINA = "https://finance.sina.com.cn/"
REF_EM = "https://quote.eastmoney.com/"

INDEX_LIST = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sz399905", "中证500"),
]


# --------------------------------------------------------------------------- #
# 代码规范化
# --------------------------------------------------------------------------- #
def guess_market(num: str) -> str:
    """根据股票代码推断交易所前缀。"""
    if num.startswith(("60", "68", "90", "11", "13", "50", "51", "52", "56", "58")):
        return "sh"
    if num.startswith(("00", "30", "20", "12", "15", "16", "18", "39")):
        return "sz"
    if num.startswith(("4", "8", "92")):
        return "bj"
    if num.startswith("9"):
        return "sh"
    if num.startswith("5"):
        return "sh"
    return "sz"


def normalize(item: str) -> Optional[Dict[str, str]]:
    """把 '600519' / 'sh600519' / 'SH.600519' 统一为结构化代码。"""
    if not item:
        return None
    s = str(item).strip().lower().replace(".", "")
    prefix = ""
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            prefix, s = p, s[len(p):]
            break
    if not s.isdigit() or len(s) != 6:
        if s.isdigit() and len(s) < 6:
            s = s.zfill(6)
        else:
            return None
    market = prefix or guess_market(s)
    return {"market": market, "code": s, "full": market + s}


def em_secid(market: str, code: str) -> str:
    return ("1." if market == "sh" else "0.") + code


def _is_a_share(code: str) -> bool:
    """过滤可转债/基金等非 A 股标的。"""
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(("60", "68", "00", "30", "43", "83", "87", "88", "92"))


# --------------------------------------------------------------------------- #
# 实时行情（腾讯，单次可批量）
# --------------------------------------------------------------------------- #
_QUOTE_CACHE_KEY = "quote:{}"


def _parse_quote(fields: List[str]) -> Dict[str, Any]:
    def f(idx, default=0.0):
        try:
            v = fields[idx]
            return float(v) if v not in ("", "-") else default
        except (IndexError, ValueError):
            return default

    def s(idx, default=""):
        try:
            return fields[idx] or default
        except IndexError:
            return default

    price = f(3)
    prev = f(4)
    return {
        "name": s(1),
        "code": s(2),
        "price": price,
        "prev_close": prev,
        "open": f(5),
        "volume": f(6),              # 手
        "change": f(31),
        "change_pct": f(32),
        "high": f(33),
        "low": f(34),
        "amount": f(37) * 10000,     # 元（腾讯给的是万元）
        "turnover": f(38),           # 换手率 %
        "pe_ttm": f(39),
        "amplitude": f(43),
        "float_cap": f(44) * 1e8,    # 元
        "total_cap": f(45) * 1e8,    # 元
        "pb": f(46),
        "limit_up": f(47),
        "limit_down": f(48),
        "volume_ratio": f(49),       # 量比
        "avg_price": f(51),
        "pe_dyn": f(52),
        "pe_static": f(53),
        "high_52w": f(67),
        "low_52w": f(68),
        "time": s(30),
    }


def fetch_quotes(fulls: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量实时行情。腾讯单次建议不超过 60 个代码。"""
    result: Dict[str, Dict[str, Any]] = {}
    pending: List[str] = []
    for full in fulls:
        hit = cache.get(_QUOTE_CACHE_KEY.format(full))
        if hit:
            result[full] = hit
        else:
            pending.append(full)
    for i in range(0, len(pending), 50):
        chunk = pending[i : i + 50]
        text = http_get("https://qt.gtimg.cn/q=" + ",".join(chunk), referer=REF_QQ, encoding="gbk")
        if not text:
            continue
        for line in text.split("\n"):
            line = line.strip()
            if not line or '="' not in line:
                continue
            head, payload = line.split('="', 1)
            full = head.split("v_")[-1].strip()
            fields = payload.rstrip('";').split("~")
            if len(fields) < 50:
                continue
            parsed = _parse_quote(fields)
            result[full] = parsed
            cache.set(_QUOTE_CACHE_KEY.format(full), parsed, config.TTL_QUOTE)
    return result


# --------------------------------------------------------------------------- #
# K 线（多源容错：腾讯代理域名 → 新浪 → 腾讯主域名）
# --------------------------------------------------------------------------- #
# 顺序：web.ifzq 是稳定主源，proxy 已被反爬（501）降为兜底
_QQ_KLINE_HOSTS = [
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
]


def _parse_qq_kline(payload: Any, full: str, period: str) -> List[Dict[str, Any]]:
    if not payload or payload.get("code") != 0:
        return []
    node = (payload.get("data") or {}).get(full) or {}
    rows = node.get("qfq" + period) or node.get(period) or []
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            out.append(
                {
                    "date": row[0],
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "volume": float(row[5]),          # 手
                    "amount": float(row[6]) if len(row) > 6 and row[6] not in ("", None) else None,
                }
            )
        except (ValueError, TypeError):
            continue
    return out


def _fetch_kline_qq(full: str, period: str, count: int) -> List[Dict[str, Any]]:
    for host in _QQ_KLINE_HOSTS:
        # 全局限速：K 线类请求控制在约 8 次/秒，避免触发数据源风控
        throttle("qq_kline", 0.125)
        url = "{host}?param={full},{period},,,{count},qfq".format(
            host=host, full=full, period=period, count=count
        )
        data = http_get_json(url, referer=REF_QQ)
        rows = _parse_qq_kline(data, full, period)
        if rows:
            note_upstream(False)
            return rows
        note_upstream(True)
    return []


def _fetch_kline_sina(full: str, count: int) -> List[Dict[str, Any]]:
    """新浪日 K（不复权）。volume 单位为股，统一换算为手。"""
    throttle("sina_kline", 0.125)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={full}&scale=240&ma=no&datalen={n}".format(
            full=full, n=max(30, count)
        )
    )
    data = http_get_json(url, referer=REF_SINA)
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        try:
            out.append(
                {
                    "date": str(row.get("day") or "")[:10],
                    "open": float(row.get("open")),
                    "close": float(row.get("close")),
                    "high": float(row.get("high")),
                    "low": float(row.get("low")),
                    "volume": float(row.get("volume")) / 100.0,
                    "amount": None,
                }
            )
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def _aggregate(daily: List[Dict[str, Any]], period: str) -> List[Dict[str, Any]]:
    """用日线聚合出周线 / 月线，作为长周期数据源的兜底。"""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for row in daily:
        date = row.get("date") or ""
        if len(date) < 10:
            continue
        if period == "week":
            y, m, d = int(date[:4]), int(date[5:7]), int(date[8:10])
            import datetime as _dt
            iso = _dt.date(y, m, d).isocalendar()
            key = "{}-W{:02d}".format(iso[0], iso[1])
        else:
            key = date[:7]
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)
    out = []
    for key in order:
        group = buckets[key]
        out.append(
            {
                "date": group[-1]["date"],
                "open": group[0]["open"],
                "close": group[-1]["close"],
                "high": max(r["high"] for r in group),
                "low": min(r["low"] for r in group),
                "volume": sum(r["volume"] for r in group),
                "amount": None,
            }
        )
    return out


def fetch_kline(full: str, period: str = "day", count: int = 320) -> List[Dict[str, Any]]:
    """period: day / week / month。返回按时间升序的 OHLCV 列表。"""

    def producer():
        if period == "day":
            rows = _fetch_kline_qq(full, "day", count)
            if not rows:
                rows = _fetch_kline_sina(full, count)
            return rows or None
        rows = _fetch_kline_qq(full, period, count)
        if rows:
            return rows
        # 长周期失败时用日线聚合兜底
        daily = fetch_kline(full, "day", max(320, count * 5))
        if not daily:
            return None
        agg = _aggregate(daily, period)
        return agg[-count:] if len(agg) > count else agg

    return cached("kline:{}:{}:{}".format(full, period, count), config.TTL_KLINE, producer) or []


def fetch_minute(full: str) -> Dict[str, Any]:
    """当日分时。返回 {date, points:[{t,price,volume,amount}], prev_close}。"""

    def producer():
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + full
        data = http_get_json(url, referer=REF_QQ)
        if not data or data.get("code") != 0:
            return None
        node = (data.get("data") or {}).get(full) or {}
        inner = node.get("data") or {}
        raw = inner.get("data") or []
        points = []
        for item in raw:
            parts = str(item).split()
            if len(parts) < 3:
                continue
            try:
                points.append(
                    {
                        "t": parts[0],
                        "price": float(parts[1]),
                        "volume": float(parts[2]),
                        "amount": float(parts[3]) if len(parts) > 3 else 0.0,
                    }
                )
            except (ValueError, TypeError):
                continue
        return {"date": inner.get("date", ""), "points": points}

    return cached("minute:" + full, config.TTL_QUOTE * 3, producer) or {"date": "", "points": []}


def latest_trade_date() -> str:
    """用上证指数 K 线推断最近交易日，格式 YYYYMMDD。"""

    def producer():
        rows = fetch_kline("sh000001", "day", 5)
        if rows:
            return rows[-1]["date"].replace("-", "")
        return datetime.now().strftime("%Y%m%d")

    return cached("latest_trade_date", 600, producer) or datetime.now().strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# 搜索（腾讯 smartbox，支持代码/中文/拼音）
# --------------------------------------------------------------------------- #
def search_stock(keyword: str) -> List[Dict[str, str]]:
    kw = (keyword or "").strip()
    if not kw:
        return []

    def producer():
        url = "https://smartbox.gtimg.cn/s3/?v=2&q={q}&t=all".format(q=kw)
        text = http_get(url, referer=REF_QQ, encoding="gbk")
        if not text or '="' not in text:
            return None
        payload = text.split('="', 1)[1].rstrip(';').rstrip('"')
        out = []
        for chunk in payload.split("^"):
            parts = chunk.split("~")
            if len(parts) < 5:
                continue
            market, code, name, pinyin, kind = parts[0], parts[1], parts[2], parts[3], parts[4]
            if market not in ("sh", "sz", "bj"):
                continue
            out.append(
                {"market": market, "code": code, "full": market + code, "name": name,
                 "pinyin": pinyin, "kind": kind}
            )
        return out or None

    return cached("search:" + kw, config.TTL_STATIC, producer) or []


# --------------------------------------------------------------------------- #
# 板块（腾讯行情 + 新浪资金流）
# --------------------------------------------------------------------------- #
def fetch_sector_quotes(limit: int = 60) -> List[Dict[str, Any]]:
    """行业板块行情：当日/5日/20日涨幅 + 领涨股（腾讯）。"""

    def producer():
        url = (
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/mktHs/rank"
            "?l={l}&p=1&t=01/averatio&o=0".format(l=max(20, limit))
        )
        data = http_get_json(url, referer=REF_QQ)
        if not data or data.get("code") != 0:
            return None
        out = []
        for item in data.get("data") or []:
            try:
                out.append(
                    {
                        "name": item.get("bd_name", ""),
                        "code": item.get("bd_code", ""),
                        "index": float(item.get("bd_zxj") or 0),
                        "change": float(item.get("bd_zd") or 0),
                        "change_pct": float(item.get("bd_zdf") or 0),
                        "change_5d": float(item.get("bd_zdf5") or 0),
                        "change_20d": float(item.get("bd_zdf20") or 0),
                        "leader_name": item.get("nzg_name", ""),
                        "leader_code": item.get("nzg_code", ""),
                        "leader_change_pct": float(item.get("nzg_zdf") or 0),
                    }
                )
            except (ValueError, TypeError):
                continue
        return out or None

    return cached("sector_quotes:{}".format(limit), config.TTL_MARKET, producer) or []


def fetch_sector_fundflow(kind: str = "industry", limit: int = 40) -> List[Dict[str, Any]]:
    """板块资金流（新浪）。kind: industry(行业) / concept(概念)。"""

    def producer():
        fenlei = "0" if kind == "industry" else "1"
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "MoneyFlow.ssl_bkzj_bk?page=1&num=100&sort=netamount&asc=0&fenlei=" + fenlei
        )
        data = http_get_json(url, referer=REF_SINA)
        if not data:
            return None
        out = []
        for item in data:
            try:
                out.append(
                    {
                        "name": item.get("name", ""),
                        "category": item.get("category", ""),
                        "change_pct": float(item.get("avg_changeratio") or 0) * 100,
                        "net_amount": float(item.get("netamount") or 0),
                        "in_amount": float(item.get("inamount") or 0),
                        "out_amount": float(item.get("outamount") or 0),
                        "net_ratio": float(item.get("ratioamount") or 0) * 100,
                        "leader_name": item.get("ts_name", ""),
                        "leader_code": item.get("ts_symbol", ""),
                        "leader_change_pct": float(item.get("ts_changeratio") or 0) * 100,
                    }
                )
            except (ValueError, TypeError):
                continue
        return out or None

    return cached("sector_flow:{}:{}".format(kind, limit), config.TTL_MARKET, producer) or []


# --------------------------------------------------------------------------- #
# 全市场列表（新浪，100 条/页）
# --------------------------------------------------------------------------- #
def _fetch_market_page(page: int, num: int = 100, sort: str = "symbol", asc: int = 1) -> List[Dict[str, Any]]:
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData?page={p}&num={n}&sort={s}&asc={a}&node=hs_a".format(
            p=page, n=num, s=sort, a=asc
        )
    )
    data = http_get_json(url, referer=REF_SINA)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        try:
            symbol = str(item.get("symbol") or "")
            market = symbol[:2]
            if market not in ("sh", "sz", "bj"):
                continue
            out.append(
                {
                    "full": symbol,
                    "market": market,
                    "code": str(item.get("code") or ""),
                    "name": str(item.get("name") or ""),
                    "price": float(item.get("trade") or 0),
                    "change_pct": float(item.get("changepercent") or 0),
                    "open": float(item.get("open") or 0),
                    "high": float(item.get("high") or 0),
                    "low": float(item.get("low") or 0),
                    "prev_close": float(item.get("settlement") or 0),
                    "volume": float(item.get("volume") or 0),
                    "amount": float(item.get("amount") or 0),
                    "pe": float(item.get("per") or 0),
                    "pb": float(item.get("pb") or 0),
                    "total_cap": float(item.get("mktcap") or 0) * 10000,
                    "float_cap": float(item.get("nmc") or 0) * 10000,
                    "turnover": float(item.get("turnoverratio") or 0),
                }
            )
        except (ValueError, TypeError):
            continue
    return out


def fetch_market_top(count: int = 600, sort: str = "amount") -> List[Dict[str, Any]]:
    """按某字段降序取全市场前 N 只（用于选股候选池，显著减少请求量）。"""

    def producer():
        pages = int(math.ceil(count / 100.0))
        chunks = parallel_map(
            lambda p: _fetch_market_page(p, 100, sort, 0), range(1, pages + 1), workers=8
        )
        merged: List[Dict[str, Any]] = []
        for c in chunks:
            merged.extend(c or [])
        return merged[:count] or None

    return cached("market_top:{}:{}".format(count, sort), config.TTL_MARKET * 2, producer) or []


def fetch_market_all() -> List[Dict[str, Any]]:
    """全市场 A 股快照（用于涨跌家数统计），带较长缓存并由后台线程预热。"""

    def producer():
        merged: List[Dict[str, Any]] = []
        page = 1
        while page <= 80:
            batch = parallel_map(lambda p: _fetch_market_page(p, 100, "symbol", 1),
                                 range(page, page + 8), workers=8)
            got = 0
            for c in batch:
                if c:
                    merged.extend(c)
                    got += len(c)
            if got == 0:
                break
            page += 8
        return merged or None

    return cached("market_all", 90, producer) or []


def warm_market_cache() -> None:
    """后台预热全市场快照，避免首个请求等待全量扫描。"""
    try:
        fetch_market_all()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 个股资金流（新浪，日频序列，最新在前）
# --------------------------------------------------------------------------- #
def fetch_stock_fundflow(full: str, days: int = 60) -> List[Dict[str, Any]]:
    def producer():
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "MoneyFlow.ssl_qsfx_zjlrqs?daima=" + full
        )
        data = http_get_json(url, referer=REF_SINA)
        if not isinstance(data, list):
            return None
        out = []
        for item in data[:days]:
            try:
                out.append(
                    {
                        "date": item.get("opendate", ""),
                        "close": float(item.get("trade") or 0),
                        "change_pct": float(item.get("changeratio") or 0) * 100,
                        "turnover": float(item.get("turnover") or 0),
                        "net_amount": float(item.get("netamount") or 0),
                        "net_ratio": float(item.get("ratioamount") or 0) * 100,
                        "main_net": float(item.get("r0_net") or 0),
                        "main_ratio": float(item.get("r0_ratio") or 0) * 100,
                    }
                )
            except (ValueError, TypeError):
                continue
        return out or None

    return cached("fundflow:{}:{}".format(full, days), config.TTL_KLINE, producer) or []


# --------------------------------------------------------------------------- #
# 涨停 / 跌停 / 炸板池（东方财富 push2ex）
# --------------------------------------------------------------------------- #
_POOL_PATH = {
    "up": "getTopicZTPool",
    "down": "getTopicDTPool",
    "broken": "getTopicZBPool",
}


def fetch_limit_pool(kind: str = "up", date: Optional[str] = None) -> List[Dict[str, Any]]:
    kind = kind if kind in _POOL_PATH else "up"
    date = date or latest_trade_date()

    def producer():
        url = (
            "https://push2ex.eastmoney.com/{path}"
            "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=300"
            "&sort=fbt%3Aasc&date={date}".format(path=_POOL_PATH[kind], date=date)
        )
        data = http_get_json(url, referer=REF_EM)
        if not data or data.get("rc") != 0:
            return None
        pool = ((data.get("data") or {}).get("pool")) or []
        out = []
        for item in pool:
            try:
                out.append(
                    {
                        "code": str(item.get("c") or ""),
                        "market": "sh" if item.get("m") == 1 else ("sz" if item.get("m") == 0 else "bj"),
                        "name": str(item.get("n") or ""),
                        "price": float(item.get("p") or 0) / 100.0,
                        "change_pct": float(item.get("zdp") or 0),
                        "amount": float(item.get("amount") or 0),
                        "turnover": float(item.get("hs") or 0),
                        "float_cap": float(item.get("ltsz") or 0),
                        "board_count": int(item.get("lbc") or 1),
                        "first_seal_time": int(item.get("fbt") or 0),
                        "last_seal_time": int(item.get("lbt") or 0),
                        "open_times": int(item.get("zbc") or 0),
                        "industry": str(item.get("hybk") or ""),
                        "seal_amount": float(item.get("fund") or 0),
                    }
                )
            except (ValueError, TypeError):
                continue
        return out

    return cached("pool:{}:{}".format(kind, date), config.TTL_MARKET, producer) or []


# --------------------------------------------------------------------------- #
# 龙虎榜（东方财富 datacenter）
# --------------------------------------------------------------------------- #
def fetch_billboard(code: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    flt = '(SECURITY_CODE="{}")'.format(code) if code else ""

    def producer():
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPT_DAILYBILLBOARD_DETAILSNEW"
            "&columns=SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLANATION,CHANGE_RATE,"
            "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,ACCUM_AMOUNT"
            "&pageSize={limit}&pageNumber=1&sortColumns=TRADE_DATE&sortTypes=-1{flt}".format(
                limit=limit, flt=("&filter=" + flt) if flt else ""
            )
        )
        data = http_get_json(url, referer=REF_EM)
        if not data or not data.get("success"):
            return None
        rows = ((data.get("result") or {}).get("data")) or []
        out = []
        for item in rows:
            code = str(item.get("SECURITY_CODE") or "")
            if not _is_a_share(code):
                continue
            try:
                out.append(
                    {
                        "code": str(item.get("SECURITY_CODE") or ""),
                        "name": str(item.get("SECURITY_NAME_ABBR") or ""),
                        "date": str(item.get("TRADE_DATE") or "")[:10],
                        "reason": str(item.get("EXPLANATION") or ""),
                        "change_pct": float(item.get("CHANGE_RATE") or 0),
                        "net_amount": float(item.get("BILLBOARD_NET_AMT") or 0),
                        "buy_amount": float(item.get("BILLBOARD_BUY_AMT") or 0),
                        "sell_amount": float(item.get("BILLBOARD_SELL_AMT") or 0),
                        "amount": float(item.get("ACCUM_AMOUNT") or 0),
                    }
                )
            except (ValueError, TypeError):
                continue
        return out or None

    return cached("billboard:{}:{}".format(code or "all", limit), 600, producer) or []


def data_source_status() -> Dict[str, Any]:
    from .net import error_stats

    return {"errors": error_stats(), "cache_ts": int(time.time())}
