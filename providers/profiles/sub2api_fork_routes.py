"""Sub2API fork 的站点专属路由选择。

通用协议和客户端不在这里维护站点语义；本模块只隔离已确认的 fork 路由差异。
"""

from __future__ import annotations

from urllib.parse import urlsplit

Endpoint = tuple[str, str | None]
JISUDENG_HOST_SUFFIX = "jisudeng.com"
JISUDENG_ENDPOINTS: tuple[Endpoint, ...] = (
    ("/play/checkin", "/play/checkin/status"),
)


def select_checkin_endpoints(
    base_url: str,
    defaults: tuple[Endpoint, ...],
) -> tuple[Endpoint, ...]:
    """选择站点专属签到路由；未知站点保持通用端点表。"""
    try:
        host = (urlsplit(str(base_url or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        host = ""
    if host == JISUDENG_HOST_SUFFIX or host.endswith("." + JISUDENG_HOST_SUFFIX):
        return JISUDENG_ENDPOINTS
    return defaults
