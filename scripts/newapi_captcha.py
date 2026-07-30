#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""New API fork 的「图形验证码签到」自定义脚本（纯 HTTP，不启动浏览器）。

## 用法

在管理界面把该站点的 **脚本路径** 填成：

    scripts/newapi_captcha.py

签到方式保持 `api` 即可。通用层在调站点签到接口之前会先问一句本脚本
（钩子 `do_checkin(client, log)`）；本脚本判定该站不需要验证码时返回 None，
通用层照原样走默认签到流程。

## 为什么是脚本而不是内置

验证码是**个别 fork** 的私改玩法：端点、字段名、图像形态各不相同（见下表），
而且随时可能再冒出第三种。把它写进 `providers/profiles/newapi.py` 会让通用适配器
背上一堆只服务于两三个站点的分支；抽成脚本后，新 fork 只需在这里加一行方言声明，
或者干脆另写一个脚本，通用层完全不必改动。

## 两套已知方言

| | jianzhile 系 | sheapi 系 |
|---|---|---|
| 开关 | 签到状态里的 `captcha_enabled` | `/api/status` 的 `checkin_captcha_enabled` |
| 取图 | `POST /api/user/checkin/captcha` | `GET /api/captcha?scene=checkin` |
| 图片字段 | `captcha_image` | `image` |
| 提交字段 | `captcha_answer` | `captcha_code` |

识别由 `captcha_ocr` 完成（按图像尺寸派发，两套生成器各一个识别器），逆向与验收
记录见 docs/captcha_algorithm.md。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from providers.base import ApiError, CheckinReward, contains_any, unwrap_data  # noqa: E402


@dataclass(frozen=True)
class CaptchaDialect:
    """一种签到验证码方言。"""

    key: str
    endpoint: str                 # 取图端点（相对站点根）
    method: str                   # jianzhile 系用 POST，sheapi 系用 GET
    image_keys: tuple[str, ...]   # 响应里承载 dataURL 的字段名
    answer_key: str               # 提交签到时答案的字段名


DIALECTS: tuple[CaptchaDialect, ...] = (
    CaptchaDialect("checkin_captcha", "/api/user/checkin/captcha", "POST",
                   ("captcha_image", "image"), "captcha_answer"),
    CaptchaDialect("scene_captcha", "/api/captcha?scene=checkin", "GET",
                   ("image", "captcha_image"), "captcha_code"),
)

CHECKIN_PATH = "/api/user/checkin"
STATUS_PATH = "/api/status"

# 「这次不算」的回执：换一张重试，而不是判定站点不支持。
MISSING_PATTERNS = ["请输入验证码", "captcha is required", "验证码不能为空"]
RETRY_PATTERNS = ["验证码错误", "验证码已失效", "验证码不正确", "captcha", "刷新后重试", "已过期"]
# 取图端点不存在：换下一种方言。
ENDPOINT_MISSING_PATTERNS = [
    "invalid url", "404", "not found", "page not found", "no route", "验证码场景无效",
]
# captcha_id 单次有效（实测复用直接回「验证码已失效」），所以每次重试都要重新取图。
MAX_ATTEMPTS = 4


def solver_available() -> bool:
    """识别器是否可用。

    识别本体只依赖 numpy，但解码 PNG 需要 pillow。缺任何一样都应如实报错，
    而不是崩在签到途中。
    """
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401

        from captcha_ocr import newapi_bitmap  # noqa: F401
    except Exception:
        return False
    return True


def solve_image(data_url: str) -> tuple[str, bool]:
    """按图像尺寸派发到对应识别器，返回 (答案, 是否可信)。

    用尺寸而不是端点来选识别器：端点只说明「哪个 fork」，尺寸才说明「哪套生成器」。
    某个 fork 换了生成器时，靠尺寸能自动走对，不必改接线。
    """
    import base64
    import io

    import numpy as np
    from PIL import Image

    from captcha_ocr import base64_captcha, newapi_bitmap

    payload = data_url.split(",", 1)[1] if "," in data_url else data_url
    with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
        array = np.asarray(image.convert("RGB"))
    height, width = array.shape[0], array.shape[1]

    if (width, height) == (newapi_bitmap.WIDTH, newapi_bitmap.HEIGHT):
        result = newapi_bitmap.solve_array(array)
        return result.text, result.exact
    if (width, height) == (base64_captcha.WIDTH, base64_captcha.HEIGHT):
        result = base64_captcha.solve_array(array)
        return result.text, result.exact
    raise ApiError(
        None, None,
        f"验证码尺寸 {width}×{height} 没有对应的识别器（已知 "
        f"{newapi_bitmap.WIDTH}×{newapi_bitmap.HEIGHT} 与 "
        f"{base64_captcha.WIDTH}×{base64_captcha.HEIGHT}）",
    )


def captcha_required(client: Any) -> bool:
    """站点签到是否需要图形验证码。

    两套方言把开关放在不同地方：jianzhile 系写在签到状态里（`captcha_enabled`），
    sheapi 系只在 `/api/status` 给 `checkin_captcha_enabled`。只看其中一处会漏判，
    实测就是这样一路走到用错端点、报「Invalid URL」。

    两处都读不到时返回 False —— 此时仍会在签到被拒后靠「验证码不能为空」兜底切进来。
    """
    try:
        data = unwrap_data(client.get_checkin_status_raw())
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("captcha_enabled"):
        return True
    try:
        options = unwrap_data(client.request("GET", STATUS_PATH))
    except Exception:
        options = None
    return bool(isinstance(options, dict) and options.get("checkin_captcha_enabled"))


def _fetch_via(client: Any, dialect: CaptchaDialect) -> tuple[str, str]:
    """按指定方言取一张验证码，返回 (captcha_id, dataURL)。"""
    data = unwrap_data(client.request(dialect.method, dialect.endpoint, retry_non_idempotent=True))
    if not isinstance(data, dict):
        raise ApiError(None, data, "验证码接口返回结构无法识别")
    captcha_id = str(data.get("captcha_id") or data.get("id") or "")
    image = ""
    for key in dialect.image_keys:
        image = str(data.get(key) or "")
        if image:
            break
    if not captcha_id or not image:
        raise ApiError(None, data, f"验证码接口未返回 captcha_id / {dialect.image_keys[0]}")
    return captcha_id, image


class _Fetcher:
    """逐个方言试取图，记住命中的那个。"""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.dialect: CaptchaDialect | None = None

    def fetch(self) -> tuple[CaptchaDialect, str, str]:
        if self.dialect is not None:
            captcha_id, image = _fetch_via(self.client, self.dialect)
            return self.dialect, captcha_id, image

        errors: list[str] = []
        for dialect in DIALECTS:
            try:
                captcha_id, image = _fetch_via(self.client, dialect)
            except ApiError as exc:
                missing = exc.status in {404, 405} or contains_any(
                    f"{exc.message} {exc.payload}", ENDPOINT_MISSING_PATTERNS
                )
                if not missing:
                    # 业务错误（如 401 未登录）必须原样抛出，否则会被掩盖成
                    # 「所有方言都不支持」，让人以为是站点问题。
                    raise
                errors.append(f"{dialect.endpoint}: {exc.message}")
                continue
            self.dialect = dialect
            return dialect, captcha_id, image
        raise ApiError(None, None, "站点未提供已知的签到验证码端点（" + "；".join(errors) + "）")


def captcha_checkin(client: Any, log: Any = None) -> dict[str, Any] | None:
    """取图 → 离线识别 → 带答案提交，失败则换一张重试；返回签到接口的 data。

    每次重试都必须重新取图：captcha_id 单次有效，复用会直接回「验证码已失效」。
    识别器给出 exact=False 时也主动换图 —— 取图不限次、不消耗签到机会，硬猜却会
    作废一次验证码；只有已经用到最后一次机会时才带着不确定的读数提交（总比放弃好）。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    if not solver_available():
        raise ApiError(
            None, None,
            "签到需要图形验证码，但识别器不可用（缺 numpy/pillow 或 captcha_ocr 被裁剪）。"
            "请执行 uv sync --extra dev 后重试，或在浏览器手动签到。",
        )

    fetcher = _Fetcher(client)
    last_error: ApiError | None = None
    tried: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        dialect, captcha_id, image = fetcher.fetch()
        text, exact = solve_image(image)
        if not text:
            last_error = ApiError(None, None, f"验证码识别失败（第 {attempt} 次，未提取到字符）")
            continue
        if not exact and attempt < MAX_ATTEMPTS:
            tried.append(f"{text}?")
            _log(f"验证码读数 {text} 不够可信，换一张重试（第 {attempt}/{MAX_ATTEMPTS} 次）")
            continue
        tried.append(text)
        _log(f"提交验证码读数 {text}（{dialect.key}，第 {attempt}/{MAX_ATTEMPTS} 次）")
        body = json.dumps({"captcha_id": captcha_id, dialect.answer_key: text}).encode("utf-8")
        try:
            return unwrap_data(client.request("POST", CHECKIN_PATH, body, retry_non_idempotent=True))
        except ApiError as exc:
            if contains_any(exc.message, RETRY_PATTERNS):
                last_error = exc
                continue
            raise
    detail = "、".join(tried) if tried else "无"
    raise ApiError(
        None,
        last_error.payload if last_error else None,
        f"图形验证码连续 {MAX_ATTEMPTS} 次未通过（识别结果：{detail}）"
        + (f"；末次回执：{last_error.message}" if last_error else ""),
    )


def do_checkin(client: Any, log: Any = None) -> CheckinReward | None:
    """通用层钩子：接管 newapi 的签到请求。

    返回 None 表示「本站不需要验证码，按默认流程签到」；返回 CheckinReward 表示
    已经完成签到。抛 ApiError 由通用层按 classify 归类（验证码类会归 need_verification）。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    if not captcha_required(client):
        return None

    _log("站点签到需要图形验证码，走离线识别流程")
    data = captcha_checkin(client, log=log)
    if isinstance(data, dict):
        return CheckinReward(
            quota_awarded=data.get("quota_awarded"),
            current_quota=data.get("quota"),
            raw=data,
        )
    return CheckinReward(raw=data)
