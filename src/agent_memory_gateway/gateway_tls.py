"""为 Sidecar 到 Gateway 的 HTTPS 连接建立进程内信任。"""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from urllib.parse import urlsplit


class GatewayTLSConfigurationError(RuntimeError):
    """Gateway CA 文件或地址不符合安全要求。"""


def validate_gateway_url(gateway_url: str) -> str:
    parsed = urlsplit(str(gateway_url).strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise GatewayTLSConfigurationError("GATEWAY_URL_INVALID")
    return parsed.geturl().rstrip("/")


def gateway_ssl_context(gateway_url: str) -> ssl.SSLContext | None:
    """只在 HTTPS 且显式指定 CA 文件时建立独立的校验证书上下文。"""

    gateway_url = validate_gateway_url(gateway_url)
    configured_path = os.environ.get("MEMORY_GATEWAY_CA_CERTIFICATE", "").strip()
    scheme = urlsplit(gateway_url).scheme.lower()
    if not configured_path:
        return None
    if scheme != "https":
        raise GatewayTLSConfigurationError("GATEWAY_CA_REQUIRES_HTTPS")

    certificate_path = Path(configured_path).expanduser()
    if not certificate_path.is_file():
        raise GatewayTLSConfigurationError("GATEWAY_CA_CERTIFICATE_MISSING")
    try:
        return ssl.create_default_context(cafile=str(certificate_path))
    except (OSError, ssl.SSLError) as exc:
        raise GatewayTLSConfigurationError("GATEWAY_CA_CERTIFICATE_INVALID") from exc
