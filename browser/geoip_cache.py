#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确保 Camoufox 的 GeoIP 数据库完整可用，且并发启动时不会读到半成品。

问题（CI 实测）：

    启动 Camoufox 失败：Error opening database file
    (.../site-packages/camoufox/GeoLite2-City.mmdb). Is this a valid MaxMind DB file?

Camoufox 的 ``get_geolocation`` 只用 ``MMDB_FILE.exists()` 判断是否需要下载，而
``download_mmdb()`` 直接以 ``open(MMDB_FILE, 'wb')`` 写最终路径，既没有锁也没有
原子替换。本项目批量签到是「组间并发」（ThreadPoolExecutor + 子进程），多个任务会
同时调用 ``launch_camoufox(geoip=True)``：

1. 进程 A 开始下载，文件立刻被创建但内容仍在写入（65MB，实测需数秒）；
2. 进程 B 看到 ``exists()`` 为真，直接交给 geoip2 打开这个被截断的文件；
3. geoip2 报「不是有效的 MaxMind DB」，浏览器启动失败。

同一批次里先跑完的站点日志能看到「Downloading GeoIP database」，紧随其后的站点就
报这个错，时序完全吻合。

修复策略（全部在本仓库内完成，不依赖上游改动）：
- 用同目录下的锁文件把「检查 + 下载」串成临界区，跨进程互斥；
- 先下载到临时文件，``os.replace`` 原子改名到最终路径，读者只会看到完整文件；
- 启动前校验现有文件能否被 geoip2 打开，损坏则删除重下，自动修好已被写坏的缓存。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_LOCK_STALE_SECONDS = 600.0


def _mmdb_path() -> Path | None:
    try:
        from camoufox.locale import MMDB_FILE
    except Exception:
        return None
    try:
        return Path(str(MMDB_FILE))
    except Exception:
        return None


def _database_is_valid(path: Path) -> bool:
    """用真实的 geoip2 读取器校验文件，避免只看大小而漏掉截断/损坏。"""
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        import geoip2.database

        with geoip2.database.Reader(str(path)) as reader:
            # 任意公共 IP 都能触发一次真实查表；查不到记录不代表文件坏。
            try:
                reader.city("8.8.8.8")
            except Exception as exc:
                if type(exc).__name__ == "AddressNotFoundError":
                    return True
                raise
        return True
    except Exception:
        return False


class _FileLock:
    """跨进程文件锁：用 O_CREAT|O_EXCL 创建独占锁文件。"""

    def __init__(self, path: Path, *, timeout: float = 300.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> bool:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return True
            except FileExistsError:
                # 持锁进程可能已被强杀（CI 任务超时），过期锁必须能被回收。
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > _LOCK_STALE_SECONDS:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.2)
            except OSError:
                # 目录不可写等情况：不阻塞启动，交给调用方按未加锁路径继续。
                return False

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_geoip_database(*, timeout: float = 300.0) -> str:
    """确保 GeoIP 数据库存在且完整。返回状态字符串，绝不抛异常。

    - ``"ready"``      ：已有完整数据库，未做改动；
    - ``"downloaded"`` ：本次（或等锁期间由他人）完成下载，现已可用；
    - ``"repaired"``   ：发现损坏文件，已删除并重新下载；
    - ``"unavailable"``：camoufox/geoip 不可用，交由原生流程处理；
    - ``"failed"``     ：下载或校验失败，调用方应让原生流程继续并暴露真实错误。
    """
    target = _mmdb_path()
    if target is None:
        return "unavailable"

    # 快路径：已完整则不进入临界区，避免每次启动都抢锁。
    if _database_is_valid(target):
        return "ready"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "failed"

    lock = _FileLock(target.parent / f"{target.name}.lock", timeout=timeout)
    with lock as acquired:
        if not acquired:
            # 拿不到锁时不能直接放行：并发写入期的半成品正是崩溃根源。
            # 等锁超时后再校验一次，仍不完整就报失败。
            return "ready" if _database_is_valid(target) else "failed"

        # 可能在等锁期间已由其它进程下载完成。
        if _database_is_valid(target):
            return "ready"

        repairing = target.exists()
        if repairing:
            try:
                target.unlink()
            except OSError:
                return "failed"

        try:
            from camoufox.locale import MMDB_REPO, MaxMindDownloader, geoip_allowed
            from camoufox.pkgman import webdl

            geoip_allowed()
            asset_url = MaxMindDownloader(MMDB_REPO).get_asset()
            # 下载到同目录临时文件后原子改名：其它进程要么看不到文件，
            # 要么看到的就是完整文件，绝不会读到写入中的半成品。
            handle, temp_name = tempfile.mkstemp(
                dir=str(target.parent), prefix=f"{target.name}.", suffix=".part"
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(handle, "wb") as buffer:
                    webdl(asset_url, desc="Downloading GeoIP database", buffer=buffer)
                if not _database_is_valid(temp_path):
                    return "failed"
                os.replace(str(temp_path), str(target))
            finally:
                temp_path.unlink(missing_ok=True)
        except Exception:
            return "failed"

        return "repaired" if repairing else "downloaded"


__all__ = ["ensure_geoip_database"]
