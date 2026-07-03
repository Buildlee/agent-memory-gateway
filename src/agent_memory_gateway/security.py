"""敏感信息检测工具。"""

from __future__ import annotations

import re


SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|password|passwd|secret)\b\s*[:=]\s*\S+"),
]


def has_sensitive_content(text: str) -> bool:
    """判断文本中是否疑似包含密钥、token、私钥或密码。"""

    return any(pattern.search(text or "") for pattern in SENSITIVE_PATTERNS)
