"""敏感信息和命令式记忆的统一分类器。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from typing import Iterable


SECURITY_RULE_VERSION = "2026-07-12.1"


@dataclass(frozen=True)
class SecurityFinding:
    category: str
    rule_id: str
    start: int
    end: int
    length_band: str
    fingerprint: str | None = None


@dataclass(frozen=True)
class SecurityAssessment:
    sensitive_findings: tuple[SecurityFinding, ...]
    instruction_like: bool
    instruction_rule_ids: tuple[str, ...]
    rule_version: str = SECURITY_RULE_VERSION

    @property
    def has_sensitive_content(self) -> bool:
        return bool(self.sensitive_findings)


@dataclass(frozen=True)
class _PatternRule:
    category: str
    rule_id: str
    pattern: re.Pattern[str]


_SENSITIVE_RULES = (
    _PatternRule(
        "private_key",
        "private-key-pem",
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----", re.IGNORECASE),
    ),
    _PatternRule(
        "api_token",
        "known-token-prefix",
        re.compile(
            r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
            r"sk_live_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{30,})\b"
        ),
    ),
    _PatternRule(
        "cloud_credential",
        "aws-access-key-id",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    _PatternRule(
        "bearer_token",
        "authorization-bearer",
        re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}"),
    ),
    _PatternRule(
        "session_token",
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    _PatternRule(
        "session_token",
        "cookie-header",
        re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:\s*[^\r\n]{8,}"),
    ),
    _PatternRule(
        "database_credential",
        "credential-url",
        re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^\s:/@]+:[^\s/@]+@[^\s]+"),
    ),
    _PatternRule(
        "credential",
        "credential-assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|pwd|"
            r"client[_-]?secret|session[_-]?(?:key|token)|private[_-]?key|cookie|secret)\b"
            r"\s*[:=]\s*(?P<value>[^\s,;]{6,})"
        ),
    ),
    _PatternRule(
        "recovery_secret",
        "seed-or-recovery-phrase",
        re.compile(r"(?i)\b(?:seed phrase|mnemonic|recovery code(?:s)?)\b\s*[:=]\s*(?:[a-z]{3,}\s+){5,}[a-z]{3,}"),
    ),
)


_INSTRUCTION_RULES = (
    ("ignore-prior", re.compile(r"(?i)(?:ignore|disregard).{0,24}(?:previous|prior|system|developer).{0,12}instructions?")),
    ("ignore-prior-zh", re.compile(r"(?:忽略|无视|绕过).{0,12}(?:前文|之前|系统|开发者|安全).{0,8}(?:指令|提示|规则|限制)")),
    ("override-system", re.compile(r"(?i)(?:override|replace|rewrite).{0,20}(?:system|developer).{0,10}(?:prompt|message|instruction)")),
    ("override-system-zh", re.compile(r"(?:覆盖|替换|改写).{0,12}(?:系统|开发者).{0,8}(?:提示|指令|规则)")),
    ("execute-command", re.compile(r"(?i)(?:execute|run).{0,16}(?:command|shell|powershell|bash|script)")),
    ("execute-command-zh", re.compile(r"(?:执行|运行).{0,12}(?:命令|脚本|PowerShell|Shell|工具)")),
    ("invoke-tool", re.compile(r"(?i)(?:call|invoke|use).{0,12}(?:tool|function|plugin|mcp)")),
    ("invoke-tool-zh", re.compile(r"(?:调用|使用).{0,10}(?:工具|函数|插件|MCP)")),
    ("scope-escalation", re.compile(r"(?i)(?:grant|change|set).{0,20}(?:permission|scope|role).{0,20}(?:admin|shared|global)")),
    ("scope-escalation-zh", re.compile(r"(?:修改|提升|授予|设置).{0,12}(?:权限|作用域|角色).{0,12}(?:管理员|共享|全局|shared|admin)")),
    ("scope-escalation-zh-reversed", re.compile(r"(?:权限|作用域|角色).{0,10}(?:设置|改成|提升为|授予).{0,10}(?:管理员|共享|全局|shared|admin)")),
    ("cross-workspace", re.compile(r"(?:读取|访问|导出).{0,12}(?:其他|全部|所有).{0,8}(?:用户|工作区|租户)")),
    ("impersonate-authority", re.compile(r"(?i)(?:i am|this is).{0,12}(?:the )?(?:system|administrator|developer)")),
    ("impersonate-authority-zh", re.compile(r"(?:我是|本内容来自|这是).{0,8}(?:系统|管理员|开发者).{0,8}(?:指令|消息|命令)")),
    ("prompt-tag", re.compile(r"(?i)</?(?:system|developer|assistant|tool)(?:\s|>)")),
)


_PLACEHOLDER_VALUES = re.compile(
    r"(?i)^(?:<[^>]+>|\$\{[^}]+\}|REPLACE(?:_WITH)?_.*|YOUR_.*|EXAMPLE.*|"
    r"CHANGEME|X{6,}|\*{6,}|redacted|masked)$"
)
_CODE_EXPRESSION_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\([^\s)]*\)$")
_SHELL_VARIABLE_PASSWORD_URL = re.compile(r":\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)@")


def _length_band(length: int) -> str:
    if length <= 16:
        return "1-16"
    if length <= 32:
        return "17-32"
    if length <= 64:
        return "33-64"
    if length <= 128:
        return "65-128"
    return "129+"


def _luhn_valid(candidate: str) -> bool:
    digits = [int(value) for value in re.sub(r"\D", "", candidate)]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _cn_id_valid(candidate: str) -> bool:
    value = candidate.upper()
    if not re.fullmatch(r"\d{17}[0-9X]", value):
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    return checks[sum(int(value[index]) * weights[index] for index in range(17)) % 11] == value[-1]


class SensitiveContentScanner:
    """只返回类别、位置和可选 HMAC 指纹，不把命中原文放入结果。"""

    def __init__(self, fingerprint_key: bytes | None = None) -> None:
        if fingerprint_key is not None and len(fingerprint_key) != 32:
            raise ValueError("敏感信息指纹密钥必须为 32 字节")
        self._fingerprint_key = fingerprint_key

    @classmethod
    def from_environment(cls) -> "SensitiveContentScanner":
        encoded = os.environ.get("MEMORY_SENSITIVE_FINGERPRINT_KEY", "")
        if not encoded:
            raise ValueError("缺少 MEMORY_SENSITIVE_FINGERPRINT_KEY")
        try:
            padding = "=" * (-len(encoded) % 4)
            key = base64.b64decode((encoded + padding).encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("敏感信息指纹密钥格式无效") from exc
        return cls(key)

    def _finding(self, category: str, rule_id: str, start: int, end: int, matched: str) -> SecurityFinding:
        fingerprint = None
        if self._fingerprint_key is not None:
            fingerprint = hmac.new(
                self._fingerprint_key,
                matched.strip().encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return SecurityFinding(
            category=category,
            rule_id=rule_id,
            start=start,
            end=end,
            length_band=_length_band(len(matched)),
            fingerprint=fingerprint,
        )

    def scan(self, text: str) -> tuple[SecurityFinding, ...]:
        value = str(text or "")
        findings: list[SecurityFinding] = []
        occupied: list[tuple[int, int]] = []
        for rule in _SENSITIVE_RULES:
            for match in rule.pattern.finditer(value):
                secret_value = match.groupdict().get("value") or match.group(0)
                if rule.rule_id == "credential-assignment" and _PLACEHOLDER_VALUES.fullmatch(secret_value):
                    continue
                if rule.rule_id == "credential-assignment" and _CODE_EXPRESSION_VALUE.fullmatch(secret_value):
                    continue
                if rule.rule_id == "credential-url" and _SHELL_VARIABLE_PASSWORD_URL.search(match.group(0)):
                    continue
                if any(match.start() < end and match.end() > start for start, end in occupied):
                    continue
                findings.append(
                    self._finding(rule.category, rule.rule_id, match.start(), match.end(), match.group(0))
                )
                occupied.append((match.start(), match.end()))
        for match in re.finditer(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", value):
            if _luhn_valid(match.group(0)) and not any(
                match.start() < end and match.end() > start for start, end in occupied
            ):
                findings.append(
                    self._finding("payment_card", "payment-card-luhn", match.start(), match.end(), match.group(0))
                )
                occupied.append((match.start(), match.end()))
        for match in re.finditer(r"(?<!\d)\d{17}[0-9Xx](?!\d)", value):
            if _cn_id_valid(match.group(0)) and not any(
                match.start() < end and match.end() > start for start, end in occupied
            ):
                findings.append(
                    self._finding("government_id", "cn-resident-id", match.start(), match.end(), match.group(0))
                )
        return tuple(sorted(findings, key=lambda item: (item.start, item.end, item.rule_id)))

    def assess(self, texts: Iterable[str]) -> SecurityAssessment:
        findings: list[SecurityFinding] = []
        instruction_rules: set[str] = set()
        for text in texts:
            value = str(text or "")
            findings.extend(self.scan(value))
            instruction_rules.update(rule_id for rule_id, pattern in _INSTRUCTION_RULES if pattern.search(value))
        return SecurityAssessment(
            sensitive_findings=tuple(findings),
            instruction_like=bool(instruction_rules),
            instruction_rule_ids=tuple(sorted(instruction_rules)),
        )


def has_sensitive_content(text: str) -> bool:
    """向后兼容的本机快速检查。"""

    return bool(SensitiveContentScanner().scan(text))


def is_instruction_like(text: str) -> bool:
    return SensitiveContentScanner().assess((text,)).instruction_like


def redact_sensitive_text(text: str) -> str:
    """用于异常和诊断输出；保持非敏感上下文，不返回命中值。"""

    value = str(text or "")
    findings = SensitiveContentScanner().scan(value)
    for finding in sorted(findings, key=lambda item: item.start, reverse=True):
        value = value[: finding.start] + f"[REDACTED:{finding.category}]" + value[finding.end :]
    return value
