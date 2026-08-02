from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProxyRateLimitConfigTests(unittest.TestCase):
    def test_fn_proxy_overwrites_forwarded_ip_and_gateway_explicitly_trusts_it(self) -> None:
        for name in ("Caddyfile", "Caddyfile.slim"):
            config = (ROOT / "deploy" / "fn" / name).read_text(encoding="utf-8")
            self.assertIn("header_up X-Forwarded-For {remote_host}", config)
        for name in ("compose.yaml", "compose.slim.yaml"):
            compose = (ROOT / "deploy" / "fn" / name).read_text(encoding="utf-8")
            self.assertIn('MEMORY_TRUST_PROXY_X_FORWARDED_FOR: "1"', compose)


if __name__ == "__main__":
    unittest.main()
