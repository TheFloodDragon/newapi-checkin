# -*- coding: utf-8 -*-
"""测试全局夹具：把运行期缓存重定向到临时目录。

为什么需要：token_cache 的写入路径是模块级常量 CACHE_PATH，签到链路里多处会
在「拿到新 token」后顺手落盘。任何忘了打补丁的用例都会写进仓库里的真实缓存
（实测出现过 `https://s.invalid|s` 这类测试数据污染真实签到状态）。逐个用例
monkeypatch 不可靠——持久化入口变过一次，旧补丁就静默失效了。

因此这里用 autouse 夹具兜底：无论用例是否自己打补丁，缓存都落在 tmp_path。
需要断言缓存内容的用例照常自己 monkeypatch，行为不受影响。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_token_cache(tmp_path, monkeypatch):
    """所有测试的 token 缓存一律写入临时目录，绝不落到仓库里的真实缓存。"""
    from providers import token_cache

    monkeypatch.setattr(token_cache, "CACHE_PATH", tmp_path / "token_cache.json")
    yield
