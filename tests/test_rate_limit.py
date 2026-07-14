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


if __name__ == "__main__":
    unittest.main()
