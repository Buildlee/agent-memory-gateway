"""Psycopg 有界连接池：连接耗尽时快速返回稳定错误。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator


class DatabasePoolError(RuntimeError):
    """连接池配置或运行错误。"""


class DatabasePoolBusy(DatabasePoolError):
    """连接池暂时耗尽；调用方应退避后重试。"""

    code = "DB_POOL_EXHAUSTED"


def _environment_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise DatabasePoolError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise DatabasePoolError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _environment_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise DatabasePoolError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise DatabasePoolError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


class PostgresConnectionPool:
    """封装 psycopg_pool，并隐藏底层异常和连接串。"""

    def __init__(
        self,
        dsn: str,
        *,
        name: str,
        min_size: int = 1,
        max_size: int = 4,
        timeout: float = 3.0,
        max_waiting: int = 8,
        pool_factory: Callable[..., Any] | None = None,
        busy_errors: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        if not dsn:
            raise DatabasePoolError("缺少数据库连接串")
        if not 0 <= min_size <= max_size <= 64:
            raise DatabasePoolError("连接池大小无效")
        if not 0.05 <= timeout <= 60:
            raise DatabasePoolError("连接池等待超时无效")
        if not 0 <= max_waiting <= 1024:
            raise DatabasePoolError("连接池等待队列大小无效")
        if pool_factory is None or busy_errors is None:
            try:
                from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
            except ModuleNotFoundError as exc:
                raise DatabasePoolError(
                    '缺少 PostgreSQL 连接池依赖，请安装：pip install -e ".[postgres]"'
                ) from exc
            pool_factory = pool_factory or ConnectionPool
            busy_errors = busy_errors or (PoolTimeout, TooManyRequests)
        self.name = name
        self.timeout = float(timeout)
        self._busy_errors = busy_errors
        self._pool = pool_factory(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=self.timeout,
            max_waiting=max_waiting,
            name=name,
            open=True,
        )

    @classmethod
    def from_environment(
        cls,
        dsn: str,
        *,
        name: str,
        environment_prefix: str,
        default_max_size: int,
    ) -> "PostgresConnectionPool":
        prefix = environment_prefix.rstrip("_")
        min_size = _environment_int(f"{prefix}_MIN_SIZE", 1, minimum=0, maximum=64)
        max_size = _environment_int(
            f"{prefix}_MAX_SIZE", default_max_size, minimum=1, maximum=64
        )
        timeout = _environment_float(
            f"{prefix}_TIMEOUT_SECONDS", 3.0, minimum=0.05, maximum=60.0
        )
        max_waiting = _environment_int(
            f"{prefix}_MAX_WAITING", max_size * 2, minimum=0, maximum=1024
        )
        return cls(
            dsn,
            name=name,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            max_waiting=max_waiting,
        )

    @contextmanager
    def connection(self) -> Iterator[Any]:
        try:
            with self._pool.connection(timeout=self.timeout) as connection:
                yield connection
        except self._busy_errors as exc:
            raise DatabasePoolBusy(self.name) from None

    def wait(self, timeout: float = 10.0) -> None:
        try:
            self._pool.wait(timeout=timeout)
        except self._busy_errors:
            raise DatabasePoolBusy(self.name) from None

    def close(self) -> None:
        self._pool.close()

    def stats(self) -> dict[str, int]:
        return {str(key): int(value) for key, value in self._pool.get_stats().items()}
