import threading
import unittest

from agent_memory_gateway.rate_limit import SlidingWindowRateLimiter


class RateLimitTests(unittest.TestCase):
    def test_sliding_window_rejects_then_recovers(self):
        now = [100.0]
        limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
        self.assertTrue(limiter.allow("client", limit=2, window_seconds=10))
        self.assertTrue(limiter.allow("client", limit=2, window_seconds=10))
        self.assertFalse(limiter.allow("client", limit=2, window_seconds=10))
        self.assertTrue(limiter.allow("other", limit=2, window_seconds=10))
        now[0] = 111.0
        self.assertTrue(limiter.allow("client", limit=2, window_seconds=10))

    def test_invalid_params_raise_value_error(self):
        limiter = SlidingWindowRateLimiter()
        with self.assertRaises(ValueError):
            limiter.allow("key", limit=0, window_seconds=10)
        with self.assertRaises(ValueError):
            limiter.allow("key", limit=10, window_seconds=0)
        with self.assertRaises(ValueError):
            limiter.allow("", limit=10, window_seconds=10)

    def test_concurrent_safety(self):
        now = [1000.0]
        lock = threading.Lock()
        limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
        results = []
        errors = []

        def hit():
            try:
                allowed = limiter.allow("shared", limit=5, window_seconds=60)
                with lock:
                    results.append(allowed)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=hit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(results), 5)
        self.assertEqual(len(results), 10)


class RateLimitExtendedTests(unittest.TestCase):
    def test_within_limit(self):
        limiter = SlidingWindowRateLimiter()
        for _ in range(5):
            self.assertTrue(limiter.allow("client", limit=5, window_seconds=10))

    def test_exceeds_limit(self):
        limiter = SlidingWindowRateLimiter()
        for _ in range(5):
            limiter.allow("client", limit=5, window_seconds=10)
        self.assertFalse(limiter.allow("client", limit=5, window_seconds=10))

    def test_window_sliding(self):
        now = [100.0]
        limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
        for _ in range(2):
            self.assertTrue(limiter.allow("client", limit=2, window_seconds=10))
        self.assertFalse(limiter.allow("client", limit=2, window_seconds=10))
        now[0] = 111.0
        self.assertTrue(limiter.allow("client", limit=2, window_seconds=10))

    def test_invalid_params(self):
        limiter = SlidingWindowRateLimiter()
        with self.assertRaises(ValueError):
            limiter.allow("key", limit=-1, window_seconds=10)
        with self.assertRaises(ValueError):
            limiter.allow("key", limit=0, window_seconds=10)
        with self.assertRaises(ValueError):
            limiter.allow("key", limit=5, window_seconds=0)
        with self.assertRaises(ValueError):
            limiter.allow("", limit=5, window_seconds=10)

    def test_concurrent(self):
        """10 线程打同一 key，验证不超过上限。"""
        import threading

        limiter = SlidingWindowRateLimiter()
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def attempt() -> None:
            barrier.wait()
            allowed = limiter.allow("concurrent-key", limit=5, window_seconds=10)
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(sum(results), 5)


if __name__ == "__main__":
    unittest.main()
