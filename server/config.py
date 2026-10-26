"""智投罗盘 - 全局配置。

所有配置项均可通过环境变量覆盖，便于未来从本地服务平滑迁移到云服务器：
    HOST/PORT                 监听地址与端口
    ARK_API_KEY               火山引擎方舟 API Key（云端务必用环境变量注入，不再依赖 apikey 文件）
    ARK_MODEL                 模型名，默认 doubao-seed-evolving
    ARK_BASE_URL              方舟 OpenAI 兼容接口地址
    ZHITOU_DATA_TTL           行情类数据缓存秒数
    ZHITOU_PROXY              出网代理（可选，云端出网受限时使用）
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # .../zhitou-luopan
PROJECT_DIR = APP_DIR.parent                             # .../行情
WEB_DIR = APP_DIR / "web"


def _load_api_key() -> str:
    key = os.getenv("ARK_API_KEY", "").strip()
    if key:
        return key
    for path in (APP_DIR / "apikey", PROJECT_DIR / "apikey", APP_DIR / ".ark_key"):
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return ""


HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8710"))

ARK_API_KEY = _load_api_key()
ARK_MODEL = os.getenv("ARK_MODEL", "doubao-seed-evolving")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
# doubao-seed-evolving 属长思考型模型，复杂分析的思考阶段可达数分钟，超时需放宽
ARK_TIMEOUT = int(os.getenv("ARK_TIMEOUT", "600"))

# 缓存时长（秒）。行情类数据变化快，日线类数据变化慢。
TTL_QUOTE = float(os.getenv("ZHITOU_TTL_QUOTE", "10"))
TTL_KLINE = float(os.getenv("ZHITOU_TTL_KLINE", "120"))
TTL_STATIC = float(os.getenv("ZHITOU_TTL_STATIC", "3600"))
TTL_MARKET = float(os.getenv("ZHITOU_TTL_MARKET", "30"))
TTL_CACHE_MAX = int(os.getenv("ZHITOU_CACHE_MAX", "4000"))

HTTP_TIMEOUT = int(os.getenv("ZHITOU_HTTP_TIMEOUT", "15"))
HTTP_RETRY = int(os.getenv("ZHITOU_HTTP_RETRY", "2"))
PROXY = os.getenv("ZHITOU_PROXY", "").strip() or None

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DISCLAIMER = "数据来源于公开行情接口，分析结果仅供参考，不构成任何投资建议。市场有风险，投资需谨慎。"
