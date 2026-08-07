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

## 已知验证机制

| 配置值 | `bitmap_code` | `string_captcha` | `click_shape` |
|---|---|---|---|
| 开关 | 签到状态 `captcha_enabled` | `checkin_captcha_enabled` | `captcha_checkin_enabled` |
| 类型 | 固定5位点阵字符 | base64Captcha DriverString | GoCaptcha 图形点选 |
| 取挑战 | `POST /api/user/checkin/captcha` | `GET /api/captcha?scene=checkin` | `GET /api/go-captcha-data/click-shape` |
| 验证 | `captcha_answer` | `captcha_code` | 按序点位换取 `captcha_token` |

字符图按尺寸派发给两套离线识别器；click_shape 使用 GoCaptcha 官方13形状模板、
固定调色板与一对一全局匹配。
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from providers.base import (  # noqa: E402
    ApiError,
    CheckinReward,
    USER_AGENT,
    contains_any,
    http_request,
    unwrap_data,
)


@dataclass(frozen=True)
class CaptchaDialect:
    """一种签到验证码方言。"""

    key: str
    endpoint: str                 # 取图端点（相对站点根）
    method: str                   # bitmap_code 用 POST，string_captcha 用 GET
    image_keys: tuple[str, ...]   # 响应里承载 dataURL 的字段名
    answer_key: str               # 提交签到时答案的字段名


DIALECTS: tuple[CaptchaDialect, ...] = (
    CaptchaDialect("bitmap_code", "/api/user/checkin/captcha", "POST",
                   ("captcha_image", "image"), "captcha_answer"),
    CaptchaDialect("string_captcha", "/api/captcha?scene=checkin", "GET",
                   ("image", "captcha_image"), "captcha_code"),
)
DIALECT_BY_MODE = {dialect.key: dialect for dialect in DIALECTS}

CHECKIN_PATH = "/api/user/checkin"
STATUS_PATH = "/api/status"

# GoCaptcha click-shape 方言（newapi 多功能 fork，例：api.vectorengine.ai）。
GO_CAPTCHA_DATA_PATH = "/api/go-captcha-data/click-shape"
GO_CAPTCHA_CHECK_PATH = "/api/go-captcha-check-data/click-shape"
GO_CAPTCHA_MAX_ATTEMPTS = 4

# 「这次不算」的回执：换一张重试，而不是判定站点不支持。
RETRY_PATTERNS = ["验证码错误", "验证码已失效", "验证码不正确", "captcha", "刷新后重试", "已过期"]
# 取图端点不存在：换下一种方言。
ENDPOINT_MISSING_PATTERNS = [
    "invalid url", "404", "not found", "page not found", "no route", "验证码场景无效",
]
# captcha_id 单次有效（实测复用直接回「验证码已失效」），所以每次重试都要重新取图。
MAX_ATTEMPTS = 4


def _brief(value: Any, limit: int = 300) -> str:
    """把站点回执压成单行限长文本，供日志原样输出。"""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…（共 {len(text)} 字符）"


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


def solve_image(data_url: str, log: Any = None) -> tuple[str, bool]:
    """按图像尺寸派发到对应识别器，返回 (答案, 是否可信)。

    用尺寸而不是端点来选识别器：端点只说明「哪个 fork」，尺寸才说明「哪套生成器」。
    某个 fork 换了生成器时，靠尺寸能自动走对，不必改接线。

    尺寸与识别器名要落日志：站点换了验证码生成器时，症状只是「一直识别不对」，
    没有尺寸信息根本判断不出该去重建哪套模板库。
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
        if log:
            log(f"验证码 {width}×{height} → newapi_bitmap 识别为 {result.text!r}（exact={result.exact}）")
        return result.text, result.exact
    if (width, height) == (base64_captcha.WIDTH, base64_captcha.HEIGHT):
        result = base64_captcha.solve_array(array)
        if log:
            log(f"验证码 {width}×{height} → base64_captcha 识别为 {result.text!r}（exact={result.exact}）")
        return result.text, result.exact
    raise ApiError(
        None, None,
        f"验证码尺寸 {width}×{height} 没有对应的识别器（已知 "
        f"{newapi_bitmap.WIDTH}×{newapi_bitmap.HEIGHT} 与 "
        f"{base64_captcha.WIDTH}×{base64_captcha.HEIGHT}）",
    )


def captcha_required(client: Any, log: Any = None) -> bool:
    """站点签到是否需要图形验证码。

    不同机制把开关放在不同位置：bitmap_code 写在签到状态的 `captcha_enabled`；
    string_captcha / click_shape 通常写在 `/api/status` 的签到验证码开关。只看其中
    一处会漏判并探测错误端点。

    所有开关都读不到时返回 False，让位给默认签到流程。判定过程必须落日志，否则
    用户无法区分「路由器未调用」与「公开配置没有声明验证」。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    try:
        data = unwrap_data(client.get_checkin_status_raw())
    except Exception as exc:
        _log(f"读签到状态失败（不影响后续判定）：{type(exc).__name__}: {exc}")
        data = None
    if isinstance(data, dict) and data.get("captcha_enabled"):
        _log("签到状态接口的 captcha_enabled=true → 需要图形验证码")
        return True
    if isinstance(data, dict):
        _log(f"签到状态接口未标记需要验证码（captcha_enabled={data.get('captcha_enabled')!r}）")
    try:
        options = unwrap_data(client.request("GET", STATUS_PATH))
    except Exception as exc:
        _log(f"读 {STATUS_PATH} 失败：{type(exc).__name__}: {exc}")
        options = None
    if isinstance(options, dict):
        # 缓存给 do_checkin 选择 captcha_type，避免同一轮再请求一次 /api/status。
        try:
            setattr(client, "_captcha_status_options", options)
        except Exception:
            pass
        # 不同实现的字段命名方向不一致：
        # - string_captcha 常见：checkin_captcha_enabled
        # - click_shape 常见：captcha_checkin_enabled
        flags = {
            "checkin_captcha_enabled": options.get("checkin_captcha_enabled"),
            "captcha_checkin_enabled": options.get("captcha_checkin_enabled"),
        }
        _log(f"{STATUS_PATH} 的签到验证码开关={flags!r}，captcha_type={options.get('captcha_type')!r}")
        if any(flags.values()):
            return True
    _log("两处开关均未标记需要验证码 → 让位给默认签到流程")
    return False


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

    def __init__(
        self,
        client: Any,
        log: Any = None,
        dialects: tuple[CaptchaDialect, ...] = DIALECTS,
    ) -> None:
        self.client = client
        self.dialects = dialects
        self.dialect: CaptchaDialect | None = None
        self._log = log

    def _say(self, message: str) -> None:
        if self._log:
            self._log(message)

    def fetch(self) -> tuple[CaptchaDialect, str, str]:
        if self.dialect is not None:
            captcha_id, image = _fetch_via(self.client, self.dialect)
            return self.dialect, captcha_id, image

        errors: list[str] = []
        for dialect in self.dialects:
            try:
                captcha_id, image = _fetch_via(self.client, dialect)
            except ApiError as exc:
                missing = exc.status in {404, 405} or contains_any(
                    f"{exc.message} {exc.payload}", ENDPOINT_MISSING_PATTERNS
                )
                if not missing:
                    # 业务错误（如 401 未登录）必须原样抛出，否则会被掩盖成
                    # 「所有方言都不支持」，让人以为是站点问题。
                    self._say(f"取图端点 {dialect.endpoint} 返回业务错误，停止探测：{exc.message}")
                    raise
                self._say(f"取图端点 {dialect.endpoint} 不存在（{exc.message}），试下一种方言")
                errors.append(f"{dialect.endpoint}: {exc.message}")
                continue
            self.dialect = dialect
            # 方言是「这个站属于哪个 fork」的唯一线索，命中后固定复用，必须记一行。
            self._say(f"命中验证码方言 {dialect.key}（{dialect.method} {dialect.endpoint}）")
            return dialect, captcha_id, image
        raise ApiError(None, None, "站点未提供已知的签到验证码端点（" + "；".join(errors) + "）")


def captcha_checkin(
    client: Any,
    log: Any = None,
    stats: dict[str, Any] | None = None,
    mode: str = "auto",
) -> dict[str, Any] | None:
    """取图 → 离线识别 → 带答案提交，失败则换一张重试；返回签到接口的 data。

    每次重试都必须重新取图：captcha_id 单次有效，复用会直接回「验证码已失效」。
    识别器给出 exact=False 时也主动换图 —— 取图不限次、不消耗签到机会，硬猜却会
    作废一次验证码；只有已经用到最后一次机会时才带着不确定的读数提交（总比放弃好）。

    stats 是可选的输出参数：填入本次识别用了几次、命中哪套方言、读数是否可信，
    由 do_checkin 放进 CheckinReward.extra，最终出现在批量汇总里。日志会滚走，
    汇总和结果 JSON 才是事后能回查的记录。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    def _stat(**values: Any) -> None:
        if stats is not None:
            stats.update(values)

    if not solver_available():
        raise ApiError(
            None, None,
            "签到需要图形验证码，但识别器不可用（缺 numpy/pillow 或 captcha_ocr 被裁剪）。"
            "请执行 uv sync --extra dev 后重试，或在浏览器手动签到。",
        )

    selected = DIALECT_BY_MODE.get(mode)
    dialects = (selected,) if selected is not None else DIALECTS
    fetcher = _Fetcher(client, log=_log, dialects=dialects)
    last_error: ApiError | None = None
    tried: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        dialect, captcha_id, image = fetcher.fetch()
        _log(f"第 {attempt}/{MAX_ATTEMPTS} 次取图完成（captcha_id={captcha_id[:12] or '空'}…），开始离线识别")
        text, exact = solve_image(image, log=_log)
        if not text:
            _log(f"第 {attempt}/{MAX_ATTEMPTS} 次未从图像提取到任何字符，换一张重试")
            last_error = ApiError(None, None, f"验证码识别失败（第 {attempt} 次，未提取到字符）")
            continue
        if not exact and attempt < MAX_ATTEMPTS:
            tried.append(f"{text}?")
            _log(f"验证码读数 {text} 不够可信，换一张重试（第 {attempt}/{MAX_ATTEMPTS} 次）")
            continue
        tried.append(text)
        confidence = "可信" if exact else "不可信（已是最后一次机会，仍提交）"
        _log(
            f"提交验证码读数 {text}（{dialect.key}，字段 {dialect.answer_key}，"
            f"{confidence}，第 {attempt}/{MAX_ATTEMPTS} 次）"
        )
        body = json.dumps({"captcha_id": captcha_id, dialect.answer_key: text}).encode("utf-8")
        try:
            data = unwrap_data(client.request("POST", CHECKIN_PATH, body, retry_non_idempotent=True))
        except ApiError as exc:
            # 站点回执必须原样打出来：「验证码错误」和「今日已签到」都会走到这里，
            # 只看最终 message 分不清是识别错了还是根本不该重试。
            _log(f"第 {attempt}/{MAX_ATTEMPTS} 次提交被拒：{exc.message}；原始回执：{_brief(exc.payload)}")
            if contains_any(exc.message, RETRY_PATTERNS):
                last_error = exc
                continue
            raise
        _log(f"第 {attempt}/{MAX_ATTEMPTS} 次提交通过，站点原始返回：{_brief(data)}")
        _stat(
            captcha_dialect=dialect.key,
            captcha_attempts=attempt,
            captcha_answer_exact=exact,
        )
        return data
    detail = "、".join(tried) if tried else "无"
    _log(f"验证码连续 {MAX_ATTEMPTS} 次未通过（识别结果：{detail}）")
    _stat(captcha_attempts=MAX_ATTEMPTS, captcha_failed=True)
    raise ApiError(
        None,
        last_error.payload if last_error else None,
        f"图形验证码连续 {MAX_ATTEMPTS} 次未通过（识别结果：{detail}）"
        + (f"；末次回执：{last_error.message}" if last_error else ""),
    )


def _multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----newapi-checkin-{uuid.uuid4().hex}"
    chunks = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def _go_captcha_request(client: Any, fields: dict[str, str]) -> Any:
    """提交 GoCaptcha multipart 验证；部分部署要求携带当前用户认证头。"""
    body, boundary = _multipart(fields)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": client.base_url,
        "Referer": getattr(client, "referer", client.base_url + "/"),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    auth = getattr(client, "auth", None)
    if auth:
        if getattr(auth, "new_api_user", ""):
            headers["New-Api-User"] = str(auth.new_api_user)
        if getattr(auth, "access_token", ""):
            headers["Authorization"] = f"Bearer {auth.access_token}"
        if getattr(auth, "cookie", ""):
            headers["Cookie"] = str(auth.cookie)
    return http_request(
        client.base_url + GO_CAPTCHA_CHECK_PATH,
        method="POST",
        headers=headers,
        body=body,
        proxy=str(getattr(client.site, "proxy", "") or ""),
        retry_non_idempotent=True,
        verify_ssl=getattr(client.site, "verify_ssl", True),
    )


def click_shape_checkin(
    client: Any, log: Any = None, stats: dict[str, Any] | None = None
) -> Any:
    """全自动完成 GoCaptcha click-shape，并用 captcha_token 签到。"""
    from captcha_ocr.go_captcha_shape import available, solve_challenge

    def _log(message: str) -> None:
        if log:
            log(message)

    if not available():
        raise ApiError(
            None, None,
            "站点签到使用 GoCaptcha click-shape，但当前环境缺少 "
            "opencv-python-headless 或官方形状模板；请执行 uv sync 后重试。",
        )

    last_message = ""
    for attempt in range(1, GO_CAPTCHA_MAX_ATTEMPTS + 1):
        challenge = client.request("GET", GO_CAPTCHA_DATA_PATH)
        if not isinstance(challenge, dict) or challenge.get("code") != 0:
            raise ApiError(None, challenge, f"click-shape 挑战接口异常：{_brief(challenge)}")
        captcha_key = str(challenge.get("captcha_key") or "")
        image = str(challenge.get("image_base64") or "")
        thumb = str(challenge.get("thumb_base64") or "")
        if not captcha_key or not image or not thumb:
            raise ApiError(None, challenge, "click-shape 挑战缺少 captcha_key / 图片")

        try:
            points = solve_challenge(image, thumb, log=_log)
        except Exception as exc:
            _log(f"第 {attempt}/{GO_CAPTCHA_MAX_ATTEMPTS} 张形状挑战识别失败：{exc}")
            last_message = str(exc)
            continue
        point_text = ";".join(f"{x},{y}" for x, y in points)
        _log(f"第 {attempt}/{GO_CAPTCHA_MAX_ATTEMPTS} 张形状挑战提交点位：{point_text}")
        verified = _go_captcha_request(
            client, {"key": captcha_key, "points": point_text}
        )
        if not isinstance(verified, dict) or verified.get("code") != 0:
            last_message = str(
                verified.get("message") if isinstance(verified, dict) else verified
            )
            _log(f"形状验证未通过：{_brief(verified)}，换一张重试")
            continue
        token = str(verified.get("token") or "")
        if not token:
            raise ApiError(None, verified, "click-shape 验证通过但未返回 token")

        try:
            data = unwrap_data(
                client.request(
                    "POST",
                    f"{CHECKIN_PATH}?captcha_token={quote(token)}",
                    retry_non_idempotent=True,
                )
            )
        except ApiError as exc:
            _log(f"captcha_token 签到被拒：{exc.message}；原始回执：{_brief(exc.payload)}")
            if contains_any(exc.message, RETRY_PATTERNS + ["人机验证", "验证失败"]):
                last_message = exc.message
                continue
            raise
        if stats is not None:
            stats.update(
                captcha_dialect="click_shape",
                captcha_attempts=attempt,
                captcha_points=points,
            )
        return data

    raise ApiError(
        None, None,
        f"click-shape 连续 {GO_CAPTCHA_MAX_ATTEMPTS} 次未通过"
        + (f"；末次原因：{last_message}" if last_message else ""),
    )


def _status_options(client: Any) -> dict[str, Any]:
    value = getattr(client, "_captcha_status_options", None)
    return value if isinstance(value, dict) else {}


def _reward_from_data(data: Any, stats: dict[str, Any]) -> CheckinReward:
    if isinstance(data, dict):
        return CheckinReward(
            quota_awarded=data.get("quota_awarded"),
            current_quota=data.get("quota"),
            raw=data,
            extra=stats,
        )
    return CheckinReward(raw=data, extra=stats)


def mode_checkin(client: Any, mode: str, log: Any = None) -> CheckinReward | None:
    """仅执行指定验证码机制；端点不适用时由调用方决定是否回落。"""
    stats: dict[str, Any] = {
        "checkin_source": "api+captcha",
        "verification_mode": mode,
    }
    if mode == "click_shape":
        data = click_shape_checkin(client, log=log, stats=stats)
    elif mode in DIALECT_BY_MODE:
        data = captcha_checkin(client, log=log, stats=stats, mode=mode)
    else:
        return None
    return _reward_from_data(data, stats)


def do_checkin(client: Any, log: Any = None) -> CheckinReward | None:
    """通用层钩子：接管 newapi 的签到请求。

    返回 None 表示「本站不需要验证码，按默认流程签到」；返回 CheckinReward 表示
    已经完成签到。抛 ApiError 由通用层按 classify 归类（验证码类会归 need_verification）。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    _log(f"验证码脚本已接入（识别器可用={solver_available()}），开始判定站点是否需要验证码")
    if not captcha_required(client, log=log):
        return None

    _log("站点签到需要图形验证码，走离线识别流程")
    options = _status_options(client)
    captcha_type = str(options.get("captcha_type") or "").strip().lower()
    if captcha_type == "click-shape":
        _log("检测到 click_shape，走 GoCaptcha 官方模板形状匹配")
        return mode_checkin(client, "click_shape", log=log)

    stats: dict[str, Any] = {
        "checkin_source": "api+captcha",
        "verification_mode": "auto",
    }
    data = captcha_checkin(client, log=log, stats=stats)
    _log(f"验证码签到完成，站点原始返回：{_brief(data)}")
    return _reward_from_data(data, stats)
