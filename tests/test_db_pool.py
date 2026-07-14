import os
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from agent_memory_gateway.db_pool import (
    DatabasePoolBusy,
    DatabasePoolError,
    PostgresConnectionPool,
)


class FakeBusyError(RuntimeError):
    pass


class FakePool:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    @contextmanager
    def connection(self, timeout):
        yield {"timeout": timeout}

    def wait(self, timeout):
        return None

    def close(self):
        self.closed = True

    def get_stats(self):
        return {"pool_size": 2, "pool_available": 1}


class BusyPool(FakePool):
    @contextmanager
    def connection(self, timeout):
        raise FakeBusyError("raw pool details must not escape")
        yield


class DatabasePoolTests(unittest.TestCase):
    def test_pool_is_bounded_and_returns_connection(self):
        pool = PostgresConnectionPool(
            "postgresql://example",
            name="metadata",
            min_size=1,
            max_size=4,
            timeout=0.2,
            max_waiting=8,
            pool_factory=FakePool,
            busy_errors=(FakeBusyError,),
        )
        try:
            with pool.connection() as connection:
                self.assertEqual(connection["timeout"], 0.2)
            self.assertEqual(pool.stats()["pool_size"], 2)
            self.assertEqual(pool._pool.kwargs["max_size"], 4)
            self.assertTrue(pool._pool.kwargs["open"])
        finally:
            pool.close()
        self.assertTrue(pool._pool.closed)

    def test_pool_exhaustion_fails_fast_with_stable_error(self):
        pool = PostgresConnectionPool(
            "postgresql://example",
            name="metadata",
            timeout=0.05,
            pool_factory=BusyPool,
            busy_errors=(FakeBusyError,),
        )
        started = time.monotonic()
        with self.assertRaises(DatabasePoolBusy) as raised:
            with pool.connection():
                pass
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(raised.exception.code, "DB_POOL_EXHAUSTED")
        self.assertNotIn("raw pool details", str(raised.exception))

    def test_environment_rejects_min_larger_than_max(self):
        values = {
            "MEMORY_TEST_POOL_MIN_SIZE": "5",
            "MEMORY_TEST_POOL_MAX_SIZE": "4",
        }
        with patch.dict(os.environ, values, clear=False):
            with self.assertRaises(DatabasePoolError):
                PostgresConnectionPool.from_environment(
                    "postgresql://example",
                    name="test",
                    environment_prefix="MEMORY_TEST_POOL",
                    default_max_size=4,
                )
