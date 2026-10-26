"""火山引擎方舟（Ark）大模型接入：doubao-seed-evolving，流式输出。

- 使用 OpenAI 兼容协议 /chat/completions，stream=true，逐块解析 SSE；
- API Key 默认读取项目内 apikey 文件，也支持 ARK_API_KEY 环境变量（云端推荐）；
- 对上层暴露生成器，便于 HTTP 层直接转发为 text/event-stream。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Generator, List, Optional

from . import config
from . import prompts
from .net import open_stream


class AIError(RuntimeError):
    pass


def _endpoint() -> str:
    return config.ARK_BASE_URL + "/chat/completions"


def stream_chat(
    messages: List[Dict[str, str]],
    temperature: float = 0.6,
    max_tokens: int = 2000,
) -> Generator[Dict[str, str], None, None]:
    """向模型发起流式请求。

    doubao-seed-evolving 为深度思考模型，会先输出大段 reasoning_content，
    因此这里把"思考"与"正文"分别 yield，交由前端分区展示，
    避免用户在前几分钟看不到任何反馈。
    产出形如 {"kind": "thinking"|"content", "text": "..."}。
    """
    if not config.ARK_API_KEY:
        raise AIError("未配置模型 API Key：请在 apikey 文件或环境变量 ARK_API_KEY 中提供")

    started = False
    try:
        for item in _stream_once(messages, temperature, max_tokens):
            started = True
            yield item
        return
    except Exception as exc:  # noqa: BLE001
        if started:
            raise AIError("模型输出中断：{}".format(exc))
    # 首个数据块之前失败，允许重试一次（思考阶段可能较长，需放宽）
    try:
        for item in _stream_once(messages, temperature, max_tokens):
            yield item
    except Exception as exc:  # noqa: BLE001
        raise AIError("模型调用失败：{}".format(exc))


def _stream_once(
    messages: List[Dict[str, str]], temperature: float, max_tokens: int
) -> Generator[Dict[str, str], None, None]:
    payload: Dict[str, Any] = {
        "model": config.ARK_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = open_stream(
        _endpoint(),
        payload,
        headers={"Authorization": "Bearer " + config.ARK_API_KEY},
        timeout=config.ARK_TIMEOUT,
    )
    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if reasoning:
                yield {"kind": "thinking", "text": reasoning}
            piece = delta.get("content")
            if piece:
                yield {"kind": "content", "text": piece}
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def build_messages(scene: str, context: Dict[str, Any]) -> List[Dict[str, str]]:
    system = prompts.SCENE_PROMPTS.get(scene, prompts.STOCK_PROMPT)
    template = prompts.USER_TEMPLATES.get(scene, "以下是结构化数据（JSON）：\n")
    body = json.dumps(context, ensure_ascii=False, default=str)
    if len(body) > 60000:
        body = body[:60000] + "...(数据已截断)"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": template + body},
    ]


def generate(scene: str, context: Dict[str, Any], temperature: Optional[float] = None) -> Generator[Dict[str, str], None, None]:
    """按业务场景生成流式解读（产出 thinking / content 两类分片）。"""
    messages = build_messages(scene, context)
    return stream_chat(messages, temperature=temperature if temperature is not None else 0.6)
