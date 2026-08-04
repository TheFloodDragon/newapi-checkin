"""站点脚本可选 hook 的显式契约。"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable

BrowserRunHook = Callable[[Any, Any, Any, Any], Any]
HttpCheckinHook = Callable[..., Any]
HttpExtrasHook = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class LoadedSiteScript:
    module: ModuleType
    run: BrowserRunHook | None = None
    do_checkin: HttpCheckinHook | None = None
    run_http_extras: HttpExtrasHook | None = None

    @classmethod
    def from_module(cls, module: ModuleType) -> LoadedSiteScript:
        def optional_hook(name: str) -> Callable[..., Any] | None:
            value = getattr(module, name, None)
            return value if callable(value) else None

        return cls(
            module=module,
            run=optional_hook("run"),
            do_checkin=optional_hook("do_checkin"),
            run_http_extras=optional_hook("run_http_extras"),
        )

    def require_browser_run(self) -> BrowserRunHook:
        if self.run is None:
            raise TypeError("脚本必须定义 async def run(page, context, site, helpers)")
        return self.run
