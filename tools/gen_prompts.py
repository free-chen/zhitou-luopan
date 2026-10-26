"""开发期辅助脚本：调用 doubao-seed-evolving 生成三类 AI 场景的系统提示词。
产物仅用于人工审核后固化到 server/prompts.py，不参与线上运行。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
KEY = ""
for p in [BASE.parent / "apikey", BASE.parent.parent / "apikey", BASE.parent / "zhitou-luopan" / "apikey"]:
    if p.exists():
        KEY = p.read_text(encoding="utf-8").strip()
        break
if not KEY:
    KEY = os.environ.get("ARK_API_KEY", "").strip()
URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
MODEL = "doubao-seed-evolving"

INSTRUCTION = """你是资深A股投研产品经理。请为"智投罗盘"产品设计3个AI分析场景的系统提示词(system prompt)。
要求：输出严格JSON，不要任何额外文字，结构如下：
{
  "market": "…",
  "stock": "…",
  "screener": "…"
}
每个提示词要求：
1) 角色设定为A股资深分析师；
2) 明确输入是结构化行情数据，必须只依据给定数据、不得编造数据或引用未给出的消息面；
3) 明确输出的小节结构与每节要写什么（用中文小标题）；
4) 强制包含风险提示与"不构成投资建议"；
5) 语言精炼、专业、可执行，避免空话；
6) market 面向"今日市场方向研判"，stock 面向"个股综合研判"，screener 面向"选股结果解读"。
"""


def main():
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你只输出合法JSON。"},
            {"role": "user", "content": INSTRUCTION},
        ],
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        content = content[4:] if content.lower().startswith("json") else content
    out = BASE / "generated_prompts.json"
    out.write_text(content, encoding="utf-8")
    print("saved:", out)
    print(content[:600])


if __name__ == "__main__":
    main()
