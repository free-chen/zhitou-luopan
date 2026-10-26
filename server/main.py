"""智投罗盘 HTTP 服务：REST + SSE + 静态资源。

仅依赖标准库，直接以 `python3 -m server.main` 启动；
未来迁移云服务器时只需改 HOST/PORT 并用环境变量注入 ARK_API_KEY。
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse
from socketserver import ThreadingMixIn

from . import analysis, config, datasource as ds, prompts, screener as scr
from . import ai as ai_mod
from .net import cache

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# --------------------------------------------------------------------------- #
# 选股任务管理（内存态，单机；迁移云端时替换为 Redis/任务队列即可）
# --------------------------------------------------------------------------- #
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
JOB_KEEP = 20


def _new_job() -> str:
    jid = uuid.uuid4().hex[:12]
    with _jobs_lock:
        if len(_jobs) > JOB_KEEP:
            for k in list(_jobs.keys())[: len(_jobs) - JOB_KEEP]:
                _jobs.pop(k, None)
        _jobs[jid] = {
            "id": jid,
            "status": "running",
            "done": 0,
            "total": 0,
            "result": None,
            "error": None,
            "stop": False,
            "started_at": time.time(),
        }
    return jid


def _run_job(jid: str, strategy: str, custom: Optional[Dict[str, Any]], pool_size: int) -> None:
    job = _jobs.get(jid)
    if not job:
        return

    def on_progress(done: int, total: int) -> None:
        job["done"], job["total"] = done, total

    try:
        result = scr.run_screener(
            strategy=strategy,
            custom=custom,
            pool_size=pool_size,
            on_progress=on_progress,
            should_stop=lambda: bool(job.get("stop")),
        )
        job["result"] = result
        job["status"] = "stopped" if result.get("stopped") else "done"
    except Exception as exc:  # noqa: BLE001
        job["error"] = str(exc)
        job["status"] = "error"
        traceback.print_exc()


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZhitouLuopan/1.0"

    # ---- 基础工具 ----
    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，避免刷屏
        pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message}, status)

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 4 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/")
        target = (config.WEB_DIR / rel).resolve()
        try:
            target.relative_to(config.WEB_DIR.resolve())
        except ValueError:
            self._send_error_json("非法路径", 403)
            return
        if not target.is_file():
            self._send_error_json("资源不存在: " + path, 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---- SSE ----
    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._sse_open = True

    def _sse_write(self, payload: Dict[str, Any]) -> None:
        if not getattr(self, "_sse_open", False):
            return
        data = ("data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n").encode("utf-8")
        try:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._sse_open = False

    def _sse_end(self) -> None:
        if not getattr(self, "_sse_open", False):
            return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self._sse_open = False

    # ---- 路由 ----
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/assets/"):
                self._serve_static(path[len("/assets/"):])
            elif path == "/api/health":
                self._send_json({
                    "status": "ok",
                    "model": config.ARK_MODEL,
                    "ai_ready": bool(config.ARK_API_KEY),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            elif path == "/api/search":
                self._send_json({"items": ds.search_stock(query.get("q", ""))})
            elif path == "/api/quotes":
                codes = [c.strip() for c in (query.get("codes") or "").split(",") if c.strip()]
                codes = codes[:80]
                quotes = ds.fetch_quotes(codes) if codes else {}
                items = []
                for c in codes:
                    q = quotes.get(c)
                    if not q:
                        continue
                    items.append({
                        "full": c,
                        "name": q.get("name", ""),
                        "code": q.get("code", ""),
                        "price": q.get("price", 0),
                        "change_pct": q.get("change_pct", 0),
                        "change": q.get("change", 0),
                        "amount": q.get("amount", 0),
                        "turnover": q.get("turnover", 0),
                        "volume_ratio": q.get("volume_ratio", 0),
                    })
                self._send_json({"items": items})
            elif path == "/api/stock":
                code = query.get("code", "")
                if not code:
                    self._send_error_json("缺少 code 参数")
                    return
                self._send_json(analysis.analyze_stock(code))
            elif path == "/api/market":
                self._send_json(analysis.analyze_market())
            elif path == "/api/sectors":
                self._send_json(analysis.analyze_sectors(query.get("kind", "industry")))
            elif path == "/api/strategies":
                self._send_json(scr.list_strategies())
            elif path == "/api/screener/progress":
                self._screener_progress(query.get("job_id", ""))
            else:
                self._send_error_json("未知接口: " + path, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_error_json("服务内部错误: {}".format(exc), 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/screener/start":
                body = self._read_body()
                jid = _new_job()
                threading.Thread(
                    target=_run_job,
                    args=(jid, body.get("strategy", "trend_bull"), body.get("custom"), int(body.get("pool_size", 400))),
                    daemon=True,
                ).start()
                self._send_json({"job_id": jid, "status": "running"})
            elif path == "/api/screener/stop":
                body = self._read_body()
                job = _jobs.get(body.get("job_id", ""))
                if job:
                    job["stop"] = True
                self._send_json({"ok": bool(job)})
            elif path == "/api/ai/stream":
                self._ai_stream(self._read_body())
            elif path == "/api/chat/stream":
                self._chat_stream(self._read_body())
            else:
                self._send_error_json("未知接口: " + path, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_error_json("服务内部错误: {}".format(exc), 500)

    # ---- 具体处理器 ----
    def _screener_progress(self, job_id: str) -> None:
        job = _jobs.get(job_id)
        if not job:
            self._send_error_json("任务不存在或已过期", 404)
            return
        payload: Dict[str, Any] = {
            "job_id": job_id,
            "status": job["status"],
            "done": job["done"],
            "total": job["total"],
            "error": job["error"],
        }
        if job["status"] in ("done", "stopped"):
            payload["result"] = job["result"]
        self._send_json(payload)

    def _ai_stream(self, body: Dict[str, Any]) -> None:
        scene = str(body.get("scene") or "stock")
        mode = str(body.get("mode") or "auto")

        try:
            context = self._build_ai_context(scene, body, mode)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json("构造分析上下文失败: {}".format(exc), 400)
            return

        self._start_sse()
        self._sse_write({"type": "meta", "scene": scene, "model": config.ARK_MODEL})
        try:
            for piece in ai_mod.generate(scene, context):
                self._sse_write({"type": piece.get("kind", "content"), "text": piece.get("text", "")})
            self._sse_write({"type": "done"})
        except Exception as exc:  # noqa: BLE001
            self._sse_write({"type": "error", "message": str(exc)})
        finally:
            self._sse_end()

    def _build_ai_context(self, scene: str, body: Dict[str, Any], mode: str) -> Dict[str, Any]:
        if mode == "client":
            ctx = body.get("context")
            return ctx if isinstance(ctx, dict) else {}
        if scene == "market":
            return analysis.build_market_ai_context(analysis.analyze_market())
        if scene == "screener":
            items = body.get("items")
            if not isinstance(items, list):
                items = []
            return analysis.build_screener_ai_context(str(body.get("strategy_name") or "选股"), items)
        code = str(body.get("code") or "")
        if not code:
            raise ValueError("缺少 code 参数")
        data = analysis.analyze_stock(code)
        if data.get("error"):
            raise ValueError(data["error"])
        return analysis.build_stock_ai_context(data)

    # ---- 多轮对话 ----
    def _chat_stream(self, body: Dict[str, Any]) -> None:
        raw = body.get("messages")
        if not isinstance(raw, list) or not raw:
            self._send_error_json("缺少对话内容", 400)
            return

        msgs: List[Dict[str, str]] = []
        for m in raw[-12:]:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            content = str(m.get("content") or "").strip()
            if role not in ("user", "assistant") or not content:
                continue
            msgs.append({"role": role, "content": content[:4000]})
        if not msgs:
            self._send_error_json("对话内容为空", 400)
            return

        system = prompts.CHAT_PROMPT
        try:
            snap = self._chat_context(body.get("context"))
            if snap:
                system += "\n\n【当前页面实时数据快照】\n" + snap
        except Exception:  # noqa: BLE001 - 快照失败不阻断对话
            pass

        messages = [{"role": "system", "content": system}] + msgs
        self._start_sse()
        self._sse_write({"type": "meta", "model": config.ARK_MODEL})
        try:
            for piece in ai_mod.stream_chat(messages, temperature=0.5, max_tokens=1600):
                self._sse_write({"type": piece.get("kind", "content"), "text": piece.get("text", "")})
            self._sse_write({"type": "done"})
        except Exception as exc:  # noqa: BLE001
            self._sse_write({"type": "error", "message": str(exc)})
        finally:
            self._sse_end()

    def _chat_context(self, ctx: Any) -> str:
        """按当前页面注入实时数据快照（个股优先，其次市场总览）。"""
        if not isinstance(ctx, dict):
            return ""
        code = str(ctx.get("code") or "")
        view = str(ctx.get("view") or "")
        if code:
            data = analysis.analyze_stock(code)
            if data and not data.get("error"):
                snap = analysis.build_stock_ai_context(data)
                return json.dumps(snap, ensure_ascii=False, default=str)[:9000]
            return ""
        if view == "market":
            snap = analysis.build_market_ai_context(analysis.analyze_market())
            return json.dumps(snap, ensure_ascii=False, default=str)[:9000]
        return ""


def _warmup_loop() -> None:
    ds.warm_market_cache()
    while True:
        time.sleep(60)
        try:
            ds.warm_market_cache()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    threading.Thread(target=_warmup_loop, daemon=True).start()
    server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    server.daemon_threads = True
    print("=" * 62)
    print("  智投罗盘 · A股智能研判平台")
    print("  服务地址 : http://{}:{}".format(config.HOST, config.PORT))
    print("  模型     : {} ({})".format(config.ARK_MODEL, "已配置 Key" if config.ARK_API_KEY else "未配置 Key"))
    print("  静态目录 : {}".format(config.WEB_DIR))
    print("=" * 62)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
