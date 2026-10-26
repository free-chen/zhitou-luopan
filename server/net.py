"""网络与缓存基础层：统一出网、编码处理、重试、TTL 缓存与并发抓取。

设计目标
- 零第三方依赖（仅标准库），本地/云端均可直接运行；
- 单点失败不影响整体（返回 None 由上层降级），避免一个数据源挂掉导致整页空白；
- 内存 TTL 缓存显著减少对上游接口的压力（选股需数百次请求）。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import config

_opener = urllib.request.build_opener()
if config.PROXY:
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": config.PROXY, "https": config.PROXY})
    )


def http_get(
    url: str,
    referer: str = "",
    encoding: str = "utf-8",
    timeout: Optional[int] = None,
    retry: Optional[int] = None,
) -> Optional[str]:
    """GET 文本。失败返回 None（不抛异常），由调用方决定降级策略。"""
    timeout = timeout or config.HTTP_TIMEOUT
    retry = config.HTTP_RETRY if retry is None else retry
    headers = {
        "User-Agent": config.UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    last_err = None
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with _opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode(encoding, errors="ignore")
        except Exception as exc:  # noqa: BLE001 - 上游不稳定，统一兜底
            last_err = exc
            if attempt < retry:
                time.sleep(0.25 * (attempt + 1))
    _record_error(url, last_err)
    return None


def http_get_json(
    url: str, referer: str = "", encoding: str = "utf-8", timeout: Optional[int] = None
) -> Optional[Any]:
    text = http_get(url, referer=referer, encoding=encoding, timeout=timeout)
    if not text:
        return None
    text = text.strip()
    # 兼容 JSONP：cb({...}) / var x = {...};
    if not text.startswith(("[", "{")):
        l, r = text.find("{"), text.rfind("}")
        l2, r2 = text.find("["), text.rfind("]")
        if l2 != -1 and (l == -1 or l2 < l):
            text = text[l2 : r2 + 1]
        elif l != -1:
            text = text[l : r + 1]
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        _record_error(url, "json-parse-failed")
        return None


def http_post_json(
    url: str, payload: Any, headers: Optional[Dict[str, str]] = None, timeout: Optional[int] = None
) -> Optional[str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": config.UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with _opener.open(req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        _record_error(url, exc)
        return None


def open_stream(url: str, payload: Any, headers: Optional[Dict[str, str]] = None, timeout: Optional[int] = None):
    """打开一个流式 POST 连接，返回可逐行读取的响应对象（调用方负责关闭）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": config.UA, "Accept": "text/event-stream"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    return _opener.open(req, timeout=timeout or config.ARK_TIMEOUT)


# --------------------------------------------------------------------------- #
# TTL 缓存
# --------------------------------------------------------------------------- #
class TTLCache:
    def __init__(self, maxsize: int = 4000) -> None:
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key: str) -> Any:
        with self._lock:
            item = self._data.get(key)
        if not item:
            return None
        expire_at, value = item
        if expire_at < time.time():
            with self._lock:
                self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            if len(self._data) >= self._maxsize:
                # 简单淘汰：清掉最先插入的一批
                for k in list(self._data.keys())[: self._maxsize // 10 or 1]:
                    self._data.pop(k, None)
            self._data[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


cache = TTLCache(config.TTL_CACHE_MAX)
_singleflight_lock = threading.Lock()
_singleflight: Dict[str, threading.Event] = {}


def cached(key: str, ttl: float, producer: Callable[[], Any]) -> Any:
    """缓存 + 单飞（同一 key 并发只回源一次），缓解选股时的重复抓取。"""
    hit = cache.get(key)
    if hit is not None:
        return hit
    with _singleflight_lock:
        ev = _singleflight.get(key)
        if ev is None:
            ev = threading.Event()
            _singleflight[key] = ev
            leader = True
        else:
            leader = False
    if not leader:
        ev.wait(timeout=config.HTTP_TIMEOUT + 5)
        return cache.get(key)
    try:
        value = producer()
        if value is not None:
            cache.set(key, value, ttl)
        return value
    finally:
        with _singleflight_lock:
            _singleflight.pop(key, None)
        ev.set()


def parallel_map(fn: Callable[[Any], Any], items: Iterable[Any], workers: int = 12) -> List[Any]:
    items = list(items)
    if not items:
        return []
    workers = max(1, min(workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


# --------------------------------------------------------------------------- #
# 上游限速：同类请求全局串行排队，避免触发数据源风控
# --------------------------------------------------------------------------- #
_THROTTLE_LOCK = threading.Lock()
_LAST_CALL: Dict[str, float] = {}


def throttle(key: str, min_interval: float) -> None:
    """保证同一 key 的请求间隔不小于 min_interval 秒（全局生效）。"""
    if min_interval <= 0:
        return
    with _THROTTLE_LOCK:
        now = time.time()
        wait = _LAST_CALL.get(key, 0.0) + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[key] = time.time()


_fail_streak = {"n": 0}
_fail_lock = threading.Lock()


def note_upstream(failed: bool, cooldown: float = 12.0, threshold: int = 10) -> None:
    """连续失败达到阈值时短暂冷却，防止在风控期内继续无效请求。"""
    with _fail_lock:
        if not failed:
            _fail_streak["n"] = 0
            return
        _fail_streak["n"] += 1
        if _fail_streak["n"] >= threshold:
            _fail_streak["n"] = 0
            time.sleep(cooldown)


def parallel_map_progress(
    fn: Callable[[Any], Any],
    items: List[Any],
    workers: int = 12,
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[Any]:
    """带进度回调的并发映射，供选股扫描使用。"""
    total = len(items)
    results: List[Any] = [None] * total
    done = [0]
    lock = threading.Lock()

    def task(idx_item):
        idx, item = idx_item
        if should_stop and should_stop():
            return
        try:
            value = fn(item)
        except Exception:  # noqa: BLE001
            value = None
        results[idx] = value
        if on_progress:
            with lock:
                done[0] += 1
                on_progress(done[0], total)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(task, list(enumerate(items))))
    return results


# --------------------------------------------------------------------------- #
# 简易错误观测（便于本地排查上游波动）
# --------------------------------------------------------------------------- #
_errors: Dict[str, int] = {}
_err_lock = threading.Lock()


def _record_error(url: str, err: Any) -> None:
    host = urllib.parse.urlparse(url).netloc
    with _err_lock:
        _errors[host] = _errors.get(host, 0) + 1


def error_stats() -> Dict[str, int]:
    with _err_lock:
        return dict(_errors)
