"""Local-only browser console for Memory Gateway operations."""

from __future__ import annotations

import argparse
import base64
import html
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from http import cookies
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse, urlsplit

from .admin_check import evaluate_overview
from .sidecar_daemon import SidecarDaemonError, get_shared_sidecar


MAX_ADMIN_BODY_BYTES = 131_072
SESSION_COOKIE_NAME = "memory_admin_session"
DEFAULT_LOCAL_SESSION_SECONDS = 12 * 60 * 60
MAX_PERSISTENT_SESSION_DAYS = 90


class AdminConsoleError(RuntimeError):
    """Stable local console error code."""


class LocalAdminSession:
    """One-time launch token exchanged for a signed, expiring browser cookie."""

    def __init__(
        self,
        launch_token: str | None = None,
        session_token: str | None = None,
        *,
        max_age_seconds: int = DEFAULT_LOCAL_SESSION_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not 60 <= int(max_age_seconds) <= MAX_PERSISTENT_SESSION_DAYS * 86_400:
            raise AdminConsoleError("ADMIN_SESSION_MAX_AGE_INVALID")
        self.launch_token = launch_token or secrets.token_urlsafe(32)
        self.session_token = session_token or secrets.token_urlsafe(32)
        self.max_age_seconds = int(max_age_seconds)
        self._now = now
        self._used = False
        self._lock = threading.Lock()

    def consume_launch_token(self, supplied: str) -> str | None:
        with self._lock:
            if self._used:
                return None
            if not hmac.compare_digest(supplied, self.launch_token):
                return None
            self._used = True
            issued_at = int(self._now())
            body = f"v1.{issued_at}.{secrets.token_urlsafe(18)}"
            return f"{body}.{self._signature(body)}"

    def authorized(self, cookie_header: str | None) -> bool:
        if not cookie_header:
            return False
        jar = cookies.SimpleCookie()
        try:
            jar.load(cookie_header)
        except cookies.CookieError:
            return False
        morsel = jar.get(SESSION_COOKIE_NAME)
        if morsel is None:
            return False
        parts = morsel.value.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return False
        try:
            issued_at = int(parts[1])
        except ValueError:
            return False
        age = int(self._now()) - issued_at
        if age < -300 or age > self.max_age_seconds:
            return False
        body = ".".join(parts[:3])
        return hmac.compare_digest(parts[3], self._signature(body))

    def _signature(self, body: str) -> str:
        digest = hmac.new(
            self.session_token.encode("utf-8"),
            body.encode("utf-8"),
            "sha256",
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class AdminConsoleState:
    workspace_id: str
    session: LocalAdminSession
    sidecar_factory: Callable[[], Any]
    max_heartbeat_age_seconds: int = 90
    mount_path: str = ""
    secure_cookie: bool = False


def _workspace_id(value: str | None) -> str:
    workspace_id = str(value or os.environ.get("MEMORY_DEFAULT_WORKSPACE") or "").strip()
    if not workspace_id:
        raise AdminConsoleError("WORKSPACE_ID_REQUIRED")
    if len(workspace_id) > 256:
        raise AdminConsoleError("WORKSPACE_ID_INVALID")
    return workspace_id


def _mount_path(value: str | None) -> str:
    """Validate an optional reverse-proxy mount path without accepting URLs."""

    raw = str(value or "").strip()
    if raw in {"", "/"}:
        return ""
    if not raw.startswith("/") or raw.endswith("/") or "//" in raw or "?" in raw or "#" in raw:
        raise AdminConsoleError("ADMIN_MOUNT_PATH_INVALID")
    segments = raw[1:].split("/")
    if not segments or any(not segment or not segment.replace("-", "").replace("_", "").isalnum() for segment in segments):
        raise AdminConsoleError("ADMIN_MOUNT_PATH_INVALID")
    return raw


def _public_base_url(value: str | None, mount_path: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != mount_path
    ):
        raise AdminConsoleError("ADMIN_PUBLIC_BASE_URL_INVALID")
    return raw


def _write_launch_url(path_value: str, launch_url: str) -> None:
    """Persist a short-lived launch URL only in an explicitly protected directory."""

    path = Path(path_value)
    if not path.is_absolute() or not path.name or path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise AdminConsoleError("ADMIN_LAUNCH_FILE_INVALID")
    descriptor = None
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            descriptor = None
            stream.write(launch_url)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise AdminConsoleError("ADMIN_LAUNCH_FILE_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _load_or_create_session_secret(path_value: str) -> str:
    """Load a durable signing secret without exposing it outside an owner-only file."""

    path = Path(path_value)
    if not path.is_absolute() or not path.name or path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise AdminConsoleError("ADMIN_SESSION_KEY_FILE_INVALID")
    try:
        if not path.exists():
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                descriptor = None
            if descriptor is not None:
                try:
                    secret = secrets.token_urlsafe(48)
                    os.write(descriptor, (secret + "\n").encode("utf-8"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        if path.is_symlink() or not path.is_file():
            raise AdminConsoleError("ADMIN_SESSION_KEY_FILE_INVALID")
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise AdminConsoleError("ADMIN_SESSION_KEY_FILE_PERMISSIONS_INVALID")
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AdminConsoleError("ADMIN_SESSION_KEY_FILE_READ_FAILED") from exc
    if not 32 <= len(secret) <= 256:
        raise AdminConsoleError("ADMIN_SESSION_KEY_FILE_INVALID")
    return secret


def _bounded_limit(value: Any, default: int = 30) -> int:
    try:
        limit = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise AdminConsoleError("LIMIT_INVALID") from exc
    if not 1 <= limit <= 100:
        raise AdminConsoleError("LIMIT_INVALID")
    return limit


def _required_text(payload: dict[str, Any], key: str, code: str, maximum: int = 256) -> str:
    value = str(payload.get(key) or "").strip()
    if not value or len(value) > maximum:
        raise AdminConsoleError(code)
    return value


def _required_positive_int(payload: dict[str, Any], key: str, code: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise AdminConsoleError(code)
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise AdminConsoleError(code) from exc
    if converted <= 0:
        raise AdminConsoleError(code)
    return converted


def _required_capabilities(payload: dict[str, Any], key: str, code: str) -> list[str]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise AdminConsoleError(code)
    values = sorted({str(value).strip() for value in raw if str(value).strip()})
    if (
        not values
        or len(values) > 32
        or any(
            len(value) > 128
            or not value.replace(".", "").replace("_", "").isalnum()
            for value in values
        )
    ):
        raise AdminConsoleError(code)
    return values


def _html_page(workspace_id: str, nonce: str, mount_path: str = "") -> bytes:
    escaped_workspace = html.escape(workspace_id, quote=True)
    escaped_api_base = html.escape(mount_path, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Memory Admin</title>
  <style nonce="{nonce}">
    :root {{
      color-scheme: light;
      --bg: oklch(0.99 0 0);
      --surface: oklch(0.968 0.004 260);
      --surface-2: oklch(0.945 0.006 260);
      --ink: oklch(0.22 0.018 260);
      --muted: oklch(0.44 0.018 260);
      --line: oklch(0.885 0.01 260);
      --accent: oklch(0.52 0.16 264);
      --accent-soft: oklch(0.94 0.025 264);
      --danger: oklch(0.52 0.16 28);
      --warning: oklch(0.61 0.13 78);
      --ok: oklch(0.48 0.13 150);
      --radius: 8px;
      --ease-out: cubic-bezier(.16, 1, .3, 1);
      --ease-standard: cubic-bezier(.2, 0, 0, 1);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-size: 14px;
      letter-spacing: 0;
    }}
    button, input, textarea, select {{
      font: inherit;
    }}
    button {{
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      min-height: 36px;
      border-radius: 7px;
      padding: 0 11px;
      cursor: pointer;
      transition:
        background-color 160ms var(--ease-standard),
        border-color 160ms var(--ease-standard),
        color 160ms var(--ease-standard),
        transform 160ms var(--ease-out);
    }}
    button:focus-visible, input[type="search"]:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    button:hover {{ border-color: oklch(0.72 0.03 260); background: var(--surface); }}
    button:active {{ transform: translateY(1px); }}
    button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    button.primary {{
      color: white;
      background: var(--accent);
      border-color: var(--accent);
    }}
    button.primary:hover {{
      background: oklch(0.47 0.17 264);
      border-color: oklch(0.47 0.17 264);
    }}
    button.danger {{ color: var(--danger); }}
    button.primary.danger {{
      color: white;
      background: var(--danger);
      border-color: var(--danger);
    }}
    button.primary.danger:hover {{
      background: oklch(0.46 0.17 28);
      border-color: oklch(0.46 0.17 28);
    }}
    button:disabled {{ opacity: .55; cursor: not-allowed; }}
    button[data-loading="true"] {{
      color: var(--muted);
      background: var(--surface);
    }}
    .shell {{
      min-height: 100vh;
      display: grid;
      grid-template-columns: 252px minmax(0, 1fr);
    }}
    aside {{
      border-right: 1px solid var(--line);
      background: var(--surface);
      padding: 20px 16px;
    }}
    main {{
      min-width: 0;
      padding: 28px clamp(18px, 2.4vw, 42px) 48px;
    }}
    .content {{ width: 100%; max-width: none; margin: 0; }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      letter-spacing: -.01em;
      margin-bottom: 28px;
    }}
    .mark {{
      width: 24px;
      height: 24px;
      border-radius: 7px;
      display: grid;
      place-items: center;
      color: white;
      background: var(--accent);
      font-size: 13px;
      font-weight: 700;
    }}
    .workspace {{
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
      margin-top: 2px;
    }}
    nav {{
      display: grid;
      gap: 3px;
    }}
    .nav-button {{
      width: 100%;
      justify-content: flex-start;
      border-color: transparent;
      background: transparent;
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 38px;
      padding: 0 10px;
      transition:
        background-color 180ms var(--ease-standard),
        border-color 180ms var(--ease-standard),
        color 180ms var(--ease-standard),
        transform 180ms var(--ease-out);
    }}
    .nav-button[aria-current="page"] {{
      background: white;
      border-color: var(--line);
      color: var(--accent);
    }}
    .dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--line);
    }}
    .nav-button[aria-current="page"] .dot {{ background: var(--accent); }}
    .side-note {{
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
      margin-top: 24px;
      padding: 14px 4px 0;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 20px;
      margin-bottom: 22px;
    }}
    h1 {{
      margin: 0 0 5px;
      font-size: 25px;
      line-height: 1.25;
      font-weight: 700;
      letter-spacing: -.025em;
      text-wrap: balance;
    }}
    .eyebrow {{
      color: var(--muted);
      font-size: 12px;
      margin: 0 0 6px;
    }}
    .subtle {{
      color: var(--muted);
      font-size: 13px;
    }}
    .toolbar {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .connection {{
      align-items: center;
      color: var(--muted);
      display: inline-flex;
      font-size: 12px;
      gap: 7px;
      min-height: 32px;
      padding: 0 2px;
    }}
    .connection-dot {{
      background: var(--warning);
      border-radius: 50%;
      height: 7px;
      width: 7px;
    }}
    .connection.ok .connection-dot {{ background: var(--ok); }}
    .connection.danger .connection-dot {{ background: var(--danger); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 8px;
    }}
    .metric, .panel, .review {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: white;
      transition:
        border-color 180ms var(--ease-standard),
        transform 180ms var(--ease-out),
        background-color 180ms var(--ease-standard);
    }}
    .metric {{
      padding: 12px 13px;
      min-height: 82px;
    }}
    .metric:hover, .review:hover {{
      border-color: oklch(0.78 0.025 260);
      transform: translateY(-1px);
    }}
    .metric .label, .label {{
      color: var(--muted);
      font-size: 12px;
    }}
    .metric .value {{
      font-size: 25px;
      line-height: 1.1;
      margin-top: 10px;
      font-weight: 720;
    }}
    .panel {{
      margin-top: 14px;
      overflow: hidden;
    }}
    .panel-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }}
    .panel-title {{
      font-weight: 650;
    }}
    .panel-body {{
      padding: 14px;
      overflow-x: auto;
    }}
    .overview-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.48fr) minmax(290px, .92fr);
      gap: 14px;
    }}
    .overview-layout .panel:first-child {{ margin-top: 0; }}
    .overview-copy {{
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.55;
      max-width: 66ch;
    }}
    .priority-list, .activity-list, .memory-list {{ display: grid; }}
    .priority-row, .activity-row, .memory-row {{
      align-items: start;
      border-bottom: 1px solid var(--line);
      display: grid;
      gap: 12px;
      padding: 12px 0;
    }}
    .priority-row {{ grid-template-columns: minmax(0, 1fr) auto; }}
    .activity-row {{ grid-template-columns: minmax(0, 1fr) auto; }}
    .memory-row {{ grid-template-columns: minmax(0, 1fr) auto; }}
    .priority-row:first-child, .activity-row:first-child, .memory-row:first-child {{ padding-top: 0; }}
    .priority-row:last-child, .activity-row:last-child, .memory-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
    .row-title {{ font-weight: 650; line-height: 1.4; }}
    .row-copy {{ color: var(--muted); font-size: 13px; line-height: 1.5; margin-top: 3px; }}
    .row-time {{ color: var(--muted); font-size: 12px; text-align: right; white-space: nowrap; }}
    .memory-search {{
      align-items: center;
      display: flex;
      gap: 8px;
    }}
    .memory-search button {{
      flex-shrink: 0;
      min-width: 64px;
      white-space: nowrap;
    }}
    input[type="search"], select {{
      background: white;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--ink);
      min-height: 36px;
      min-width: 0;
      padding: 0 11px;
    }}
    input[type="search"] {{
      width: min(520px, 100%);
    }}
    select {{ min-width: 160px; }}
    .memory-meta {{
      align-items: center;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      font-size: 12px;
      gap: 6px;
      margin-top: 8px;
    }}
    .memory-content {{
      line-height: 1.6;
      margin-top: 8px;
      max-width: 78ch;
      white-space: pre-wrap;
    }}
    .error-state {{
      background: oklch(0.98 0.012 78);
      border: 1px solid oklch(0.88 0.055 78);
      border-radius: var(--radius);
      margin-bottom: 14px;
      padding: 14px;
    }}
    .error-state[hidden] {{ display: none; }}
    .error-title {{ font-weight: 700; }}
    .error-copy {{ color: oklch(0.37 0.055 78); line-height: 1.55; margin: 4px 0 12px; max-width: 72ch; }}
    .sr-only {{
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      height: 1px;
      overflow: hidden;
      position: absolute;
      white-space: nowrap;
      width: 1px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0 8px;
      background: white;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .badge.ok {{ color: var(--ok); border-color: oklch(0.84 0.05 150); background: oklch(0.965 0.025 150); }}
    .badge.warn {{ color: oklch(0.43 0.1 70); border-color: oklch(0.84 0.07 78); background: oklch(0.965 0.035 78); }}
    .badge.danger {{ color: var(--danger); border-color: oklch(0.84 0.06 28); background: oklch(0.965 0.025 28); }}
    .view {{ display: none; }}
    .view.active {{
      display: block;
      animation: view-in 190ms var(--ease-out);
    }}
    .review {{
      padding: 14px;
      margin-bottom: 12px;
    }}
    .review-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .review-content {{
      white-space: pre-wrap;
      line-height: 1.55;
      padding: 10px 0;
      max-width: 78ch;
    }}
    textarea {{
      width: 100%;
      min-height: 96px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 9px 10px;
      color: var(--ink);
      background: white;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .split {{
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, .9fr);
      gap: 12px;
    }}
    .empty {{
      padding: 22px 14px;
      color: var(--muted);
      text-align: center;
    }}
    .toast {{
      position: fixed;
      right: 24px;
      top: 18px;
      z-index: 40;
      width: min(420px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      background: var(--accent-soft);
      color: var(--ink);
      opacity: 0;
      pointer-events: none;
      transform: translateY(-8px);
      transition:
        opacity 180ms var(--ease-standard),
        transform 180ms var(--ease-out);
    }}
    .toast.visible {{
      opacity: 1;
      pointer-events: auto;
      transform: translateY(0);
    }}
    .skeleton {{
      height: 14px;
      border-radius: 999px;
      background: var(--surface-2);
      margin: 8px 0;
      animation: pulse 900ms var(--ease-standard) infinite alternate;
    }}
    .confirm-dialog {{
      border: 0;
      padding: 0;
      background: transparent;
    }}
    .confirm-dialog::backdrop {{
      background: oklch(0.2 0.02 260 / .28);
    }}
    .confirm-sheet {{
      width: min(420px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: 12px;
      background: white;
      color: var(--ink);
      padding: 16px;
      box-shadow: 0 8px 12px oklch(0.2 0.02 260 / .12);
      animation: dialog-in 170ms var(--ease-out);
    }}
    .dialog-title {{
      font-weight: 700;
      font-size: 15px;
      margin-bottom: 6px;
    }}
    .dialog-message {{
      color: var(--muted);
      line-height: 1.55;
      margin: 0;
    }}
    .dialog-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 16px;
    }}
    @keyframes view-in {{
      from {{ opacity: 0; transform: translateY(6px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes dialog-in {{
      from {{ opacity: 0; transform: translateY(8px) scale(.985); }}
      to {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    @keyframes pulse {{
      from {{ opacity: .55; }}
      to {{ opacity: 1; }}
    }}
    /* 管理台保持冷静、紧凑的工作台节奏：层级由留白和边界承担，不依赖装饰。 */
    body {{ background: var(--bg); font-size: 14px; }}
    aside {{
      align-self: stretch;
      background: oklch(0.975 0.004 260 / .92);
      padding: 24px 16px 18px;
    }}
    .brand {{ margin-bottom: 30px; }}
    .mark {{
      border-radius: 8px;
      box-shadow: 0 3px 8px oklch(0.52 0.16 264 / .22);
    }}
    .nav-button {{
      border-radius: 8px;
      min-height: 40px;
    }}
    .nav-button[aria-current="page"] {{
      box-shadow: 0 1px 2px oklch(0.22 0.018 260 / .04);
      font-weight: 650;
    }}
    main {{ padding-top: 32px; }}
    .content {{ width: 100%; max-width: none; }}
    .topbar {{ margin-bottom: 26px; }}
    h1 {{ font-size: 28px; }}
    .overview-copy {{ font-size: 14px; margin-bottom: 18px; }}
    .grid {{ gap: 10px; }}
    .metric {{ min-height: 118px; padding: 0; }}
    button.metric-button {{
      align-items: stretch;
      background: transparent;
      border: 0;
      border-radius: inherit;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 116px;
      padding: 15px;
      text-align: left;
      width: 100%;
    }}
    button.metric-button:hover {{
      background: linear-gradient(135deg, var(--accent-soft), transparent 70%);
      border: 0;
    }}
    .metric-top, .metric-bottom, .cell-meta, .capability-list {{
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
    }}
    .metric-arrow {{
      color: var(--accent);
      font-size: 16px;
      line-height: 1;
      margin-left: auto;
      transition: transform 160ms var(--ease-out);
    }}
    .metric-button:hover .metric-arrow {{ transform: translateX(2px); }}
    .metric .value {{ margin-top: 0; }}
    .metric-caption {{ color: var(--muted); font-size: 12px; line-height: 1.35; }}
    .panel {{
      border-radius: 10px;
      box-shadow: 0 1px 1px oklch(0.22 0.018 260 / .025);
    }}
    .panel-head {{ padding: 13px 15px; }}
    .panel-body {{ padding: 15px; }}
    .compact-link {{ min-height: 32px; padding: 0 9px; }}
    .overview-layout {{ grid-template-columns: minmax(0, 1.56fr) minmax(320px, .94fr); }}
    .priority-row, .activity-row, .memory-row {{ padding: 13px 0; }}
    .row-title {{ font-weight: 670; }}
    table {{ font-size: 13px; min-width: 720px; }}
    th, td {{ padding: 12px 10px; }}
    tbody tr {{ transition: background-color 140ms var(--ease-standard); }}
    tbody tr:hover {{ background: oklch(0.975 0.008 264); }}
    .cell-title {{ font-weight: 650; line-height: 1.4; }}
    .online-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: oklch(0.55 0.15 150); margin-right: 8px; vertical-align: middle; }}
    .online-dot.off {{ background: oklch(0.7 0 0); }}
    .badge.offline {{ background: oklch(0.88 0.02 260); color: oklch(0.45 0.02 260); }}
    .cell-copy, .cell-meta {{ color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 4px; }}
    .cell-stack {{ display: grid; gap: 4px; min-width: 150px; }}
    .capability-list {{ display: flex; flex-wrap: wrap; gap: 5px; max-width: 31rem; }}
    .capability-list .badge {{ max-width: 100%; overflow-wrap: anywhere; white-space: normal; }}
    .record-details {{ color: var(--muted); font-size: 12px; margin-top: 7px; }}
    .record-details summary {{ cursor: pointer; width: fit-content; }}
    .record-details code {{ display: block; margin-top: 6px; }}
    .muted-divider {{ color: var(--line); }}
    .section-tools {{
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 12px 15px;
      border-bottom: 1px solid var(--line);
      background: oklch(0.985 0.002 260);
    }}
    .section-tools input[type="search"] {{ width: min(360px, 100%); }}
    .device-manage {{ position: relative; margin-top: 10px; }}
    .device-manage summary {{
      align-items: center;
      cursor: pointer;
      display: inline-flex;
      min-height: 34px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: white;
      list-style: none;
      user-select: none;
    }}
    .device-manage summary::-webkit-details-marker {{ display: none; }}
    .device-manage summary:hover {{ background: var(--surface); }}
    .device-manage summary:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .device-manage[open] summary {{ border-color: oklch(0.72 0.03 260); }}
    .device-editor {{
      display: grid;
      gap: 14px;
      position: absolute;
      right: 0;
      top: calc(100% + 6px);
      width: min(560px, 88vw);
      margin-top: 12px;
      padding: 18px 20px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
      box-shadow: 0 8px 24px oklch(0 0 0 / 0.08);
      z-index: 200;
    }}
    .permission-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 8px 16px;
    }}
    .permission-option {{ align-items: center; display: flex; gap: 10px; min-height: 34px; padding: 4px 0; }}
    .danger-zone {{ border-top: 1px solid var(--line); padding-top: 12px; }}
    .danger-zone .row-copy {{ max-width: 72ch; }}
    .source-cell {{ min-width: 220px; }}
    .source-summary {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }}

        .page-size-select {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 13px;
      background: white;
    }}
    .page-nav {{
      align-items: center;
      display: flex;
      gap: 12px;
      justify-content: center;
      padding: 14px 0 4px;
      font-size: 13px;
      color: var(--muted);
    }}
    .page-nav button {{
      min-width: 80px;
      padding: 5px 14px;
    }}
    .page-nav button:disabled {{
      opacity: 0.35;
      cursor: default;
    }}
    .page-nav span {{
      min-width: 100px;
      text-align: center;
    }}
    .toolbar-count {{ margin-left: auto; }}
    @media (max-width: 860px) {{
      .shell {{ grid-template-columns: 1fr; }}
      aside {{
        position: sticky;
        top: 0;
        z-index: 2;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }}
      nav {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .nav-button {{ justify-content: center; min-height: 44px; padding: 0 6px; }}
      .nav-button .dot {{ display: none; }}
      main {{ padding: 18px 16px 34px; }}
      .topbar {{ align-items: stretch; flex-direction: column; }}
      .split, .overview-layout {{ grid-template-columns: 1fr; }}
      .memory-search {{ align-items: stretch; flex-direction: column; }}
      input[type="search"] {{ min-height: 44px; width: 100%; }}
      select {{ min-height: 44px; width: 100%; }}
      .section-tools {{ align-items: stretch; flex-direction: column; }}
      .toolbar-count {{ margin-left: 0; width: fit-content; }}
      .device-editor {{ width: min(560px, calc(100vw - 48px)); right: auto; left: 0; }}
      th, td {{ padding: 10px 7px; }}
      .toast {{
        left: 16px;
        right: 16px;
        top: 14px;
        width: auto;
      }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{
        scroll-behavior: auto !important;
        transition-duration: 0.001ms !important;
        animation-duration: 0.001ms !important;
      }}
      .view.active, .confirm-sheet, .skeleton {{ animation: none; }}
    }}
  </style>
</head>
<body data-workspace="{escaped_workspace}" data-api-base="{escaped_api_base}">
  <div class="shell">
    <aside>
      <div class="brand">
        <div class="mark">M</div>
        <div>
          <div>Memory Admin</div>
          <div class="workspace">{escaped_workspace}</div>
        </div>
      </div>
      <nav aria-label="管理页">
        <button class="nav-button" data-view="overview" aria-current="page"><span class="dot"></span>概览</button>
        <button class="nav-button" data-view="memories"><span class="dot"></span>记忆</button>
        <button class="nav-button" data-view="graph"><span class="dot"></span>图谱</button>
        <button class="nav-button" data-view="reviews"><span class="dot"></span>审核</button>
        <button class="nav-button" data-view="devices"><span class="dot"></span>设备与权限</button>
        <button class="nav-button" data-view="runtime"><span class="dot"></span>运行</button>
        <button class="nav-button" data-view="activity"><span class="dot"></span>活动</button>
      </nav>
      <div class="side-note">管理页通过受控 Sidecar 读取已授权工作区。浏览器不会保存 Gateway 凭据。</div>
    </aside>
    <main>
      <div class="content">
        <div class="topbar">
          <div>
            <p class="eyebrow">工作区管理台</p>
            <h1 id="page-title" tabindex="-1">共享记忆管理</h1>
            <div class="subtle" id="page-subtitle">当前工作区：{escaped_workspace}</div>
          </div>
          <div class="toolbar">
            <span id="connection" class="connection" role="status" aria-live="polite"><span class="connection-dot"></span><span id="connection-label">正在读取状态</span></span>
            <button id="refresh">刷新状态</button>
          </div>
        </div>
        <div id="toast" class="toast" role="status" aria-live="polite"></div>
        <section id="overview" class="view active">
          <div id="load-error" class="error-state" role="alert" hidden>
            <div class="error-title" id="load-error-title">暂时无法读取管理数据</div>
            <p class="error-copy" id="load-error-copy"></p>
            <button id="load-error-refresh">重新读取</button>
          </div>
          <div class="overview-layout">
            <div>
              <p class="overview-copy">先处理需要人工确认的记忆，再核对投递状态与设备授权。每个状态卡都可以直接进入对应页面。</p>
              <div class="grid" id="metrics"></div>
              <div class="panel">
                <div class="panel-head"><div class="panel-title">现在需要处理</div><span id="priority-badge" class="badge">读取中</span></div>
                <div class="panel-body"><div id="priority-list" class="priority-list"></div></div>
              </div>
              <div class="panel">
                <div class="panel-head"><div class="panel-title">健康检查</div><span id="health-badge" class="badge">读取中</span></div>
                <div class="panel-body" id="health-panel"></div>
              </div>
            </div>
            <div>
              <div class="panel">
                <div class="panel-head"><div class="panel-title">近期活动</div><button class="compact-link" data-view-link="activity">查看全部</button></div>
                <div class="panel-body"><div id="overview-audit-list" class="activity-list"></div></div>
              </div>
              <div class="panel">
                <div class="panel-head"><div class="panel-title">访问边界</div></div>
                <div class="panel-body"><div class="row-copy">页面只通过受控 Sidecar 请求已授权工作区。不会显示设备公钥、Gateway 凭据、刷新凭据或数据库连接信息。</div></div>
              </div>
            </div>
          </div>
        </section>
        <section id="memories" class="view">
          <div class="panel">
            <div class="panel-head"><div><div class="panel-title">共享记忆库</div><div class="subtle">当前工作区内所有已确认的记忆。输入关键词可搜索，留空则浏览全部。</div></div></div>
            <div class="section-tools">
              <form id="memory-search-form" class="memory-search">
                <label class="sr-only" for="memory-query">记忆检索关键词</label>
                <input id="memory-query" type="search" minlength="2" maxlength="256" placeholder="搜索记忆（留空则浏览全部）">
                <button class="primary" type="submit">搜索</button>
              </form>
              <label class="sr-only" for="memory-page-size">每页条数</label>
              <select id="memory-page-size" class="page-size-select">
                <option value="10">10 条/页</option>
                <option value="20" selected>20 条/页</option>
                <option value="50">50 条/页</option>
              </select>
              <span id="memory-result-count" class="badge toolbar-count">等待加载</span>
            </div>
          </div>
          <div class="panel">
            <div class="panel-body"><div id="memory-results" class="empty">打开记忆页自动加载。搜索不会修改记忆或同步队列。</div></div>
            <div class="section-tools" id="memory-pagination">
              <button id="memory-prev" disabled>上一页</button>
              <span id="memory-page-info" class="badge">第 1 页</span>
              <button id="memory-next" disabled>下一页</button>
            </div>
          </div>
        </section>
        <section id="graph" class="view">
          <div class="panel">
            <div class="panel-head"><div><div class="panel-title">记忆关系图谱</div><div class="subtle">当前工作区内记忆与设备、Agent 的关联网络。虚线表示取代关系。</div></div></div>
            <div class="panel-body"><div id="graph-container"><div class="empty">正在加载图谱…</div></div></div>
          </div>
        </section>
        <section id="reviews" class="view">
          <div id="review-list"></div>
        </section>
        <section id="devices" class="view">
          <div class="panel">
            <div class="panel-head"><div><div class="panel-title">设备与权限</div><div class="subtle">查看设备来源、调整当前工作区能力，或撤销失去信任的 Agent 与设备。所有变更都会写入审计。</div></div></div>
            <div class="panel-body" id="device-list"></div>
          </div>
        </section>
        <section id="runtime" class="view">
          <div class="panel">
            <div class="panel-head"><div><div class="panel-title">同步与投递</div><div class="subtle">这里只读展示当前异常数量；不会自动重放或清理事件。</div></div></div>
            <div class="panel-body" id="delivery-summary"></div>
          </div>
          <div class="split">
          <div class="panel">
            <div class="panel-head"><div class="panel-title">未处理死信</div></div>
            <div class="panel-body" id="dead-letter-list"></div>
          </div>
          </div>
        </section>
        <section id="activity" class="view">
          <div class="panel">
            <div class="panel-head"><div><div class="panel-title">近期活动</div><div class="subtle">按设备、Agent 或操作筛选近期记录；不显示记忆正文或敏感详情。点击目标引用可查看关联记忆。</div></div></div>
            <div class="section-tools">
              <label class="sr-only" for="activity-query">搜索活动</label>
              <input id="activity-query" type="search" placeholder="搜索设备、Agent、操作或目标">
              <label class="sr-only" for="activity-result">筛选结果</label>
              <select id="activity-result">
                <option value="">全部结果</option>
                <option value="ok">成功与已完成</option>
                <option value="warn">等待或需关注</option>
                <option value="danger">拒绝、撤销或错误</option>
              </select>
              <label class="sr-only" for="activity-page-size">每页条数</label>
              <select id="activity-page-size" class="page-size-select">
                <option value="10">10 条/页</option>
                <option value="20" selected>20 条/页</option>
                <option value="50">50 条/页</option>
              </select>
              <span id="activity-count" class="badge toolbar-count">0 条记录</span>
            </div>
            <div class="panel-body" id="audit-list"></div>
            <div class="section-tools" id="activity-pagination">
              <button id="activity-prev" disabled>上一页</button>
              <span id="activity-page-info" class="badge">第 1 页</span>
              <button id="activity-next" disabled>下一页</button>
            </div>
          </div>
        </section>
      </div>
    </main>
  </div>
  <dialog id="confirm-dialog" class="confirm-dialog">
    <form method="dialog" class="confirm-sheet">
      <div class="dialog-title" id="confirm-title">确认操作</div>
      <p class="dialog-message" id="confirm-message"></p>
      <div class="dialog-actions">
        <button value="cancel">取消</button>
        <button id="confirm-accept" class="primary" value="confirm">确认</button>
      </div>
    </form>
  </dialog>
  <script nonce="{nonce}">
    const state = {{
      workspaceId: document.body.dataset.workspace,
      apiBase: document.body.dataset.apiBase || "",
      reviews: [],
      latestOperation: null,
      overview: null,
      audit: [],
      devices: [],
      capabilityCatalog: []
    }};

    const actionNames = {{
      confirm: "确认原文",
      confirm_edit: "按编辑确认",
      retain_both: "保留双方",
      supersede: "取代冲突记忆",
      reject: "拒绝候选",
      archive: "归档候选"
    }};

    const auditActionNames = {{
      "auth.workspace.capabilities.update": "更新工作区权限",
      "auth.agent.revoke": "撤销 Agent",
      "auth.device.revoke": "撤销设备",
      "auth.device.pair": "设备完成配对",
      "review.created": "创建审核候选",
      "review.confirm": "确认候选记忆",
      "review.confirm_edit": "编辑并确认候选",
      "review.retain_both": "保留冲突双方",
      "review.supersede": "取代冲突记忆",
      "review.reject": "拒绝候选",
      "review.archive": "归档候选",
      "review.revert": "撤销审核操作",
      "event.accepted": "接收记忆事件",
      "event.applied": "应用记忆事件",
      "event.rejected_sensitive": "拒绝敏感内容",
      "auth.workspace.bind": "工作区绑定",
      "auth.pairing_code.create": "创建配对码",
      "capability_granted": "授予能力",
      "crystal.rebuilt": "重建结晶记忆"
    }};

    const labels = {{
      overview: ["共享记忆管理", "当前工作区：" + state.workspaceId],
      memories: ["共享记忆库", "浏览所有共享记忆，按关键词检索"],
      graph: ["记忆关系图谱", "查看记忆、设备与 Agent 之间的关联"],
      reviews: ["审核候选", "只处理需要人工判断的候选记忆"],
      devices: ["设备与权限", "查看设备、Agent 和工作区能力，不显示凭据"],
      runtime: ["运行", "查看同步、重试和死信的只读状态"],
      activity: ["活动", "查看近期管理与审核记录，不显示正文或敏感详情"]
    }};

    function escapeHTML(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }}

    function code(value) {{
      return `<code>${{escapeHTML(value || "-")}}</code>`;
    }}

    function toast(message) {{
      const node = document.getElementById("toast");
      node.textContent = message;
      node.classList.add("visible");
      window.clearTimeout(toast.timer);
      toast.timer = window.setTimeout(() => node.classList.remove("visible"), 5200);
    }}

    const confirmDialog = document.getElementById("confirm-dialog");
    const confirmAccept = document.getElementById("confirm-accept");
    let pendingConfirmation = null;

    function askConfirmation(action, item, targetRef, onConfirm) {{
      const actionLabel = actionNames[action] || action;
      const title = `${{actionLabel}}？`;
      const targetText = targetRef ? `目标引用：${{targetRef}}` : `候选：${{item.review_id}}`;
      document.getElementById("confirm-title").textContent = title;
      document.getElementById("confirm-message").textContent =
        `${{targetText}}。这次操作会写入审核记录，并使用当前 revision 防止覆盖新状态。`;
      confirmAccept.textContent = actionLabel;
      confirmAccept.classList.toggle("danger", action === "reject" || action === "archive");
      pendingConfirmation = onConfirm;
      confirmDialog.showModal();
    }}

    function askAdminConfirmation(title, message, acceptLabel, dangerous, onConfirm) {{
      document.getElementById("confirm-title").textContent = title;
      document.getElementById("confirm-message").textContent = message;
      confirmAccept.textContent = acceptLabel;
      confirmAccept.classList.toggle("danger", Boolean(dangerous));
      pendingConfirmation = onConfirm;
      confirmDialog.showModal();
    }}

    confirmDialog.addEventListener("close", () => {{
      const confirmed = confirmDialog.returnValue === "confirm";
      const callback = pendingConfirmation;
      pendingConfirmation = null;
      if (confirmed && callback) {{
        callback();
      }}
    }});

    async function api(path, options = {{}}) {{
      const response = await fetch(state.apiBase + path, {{
        method: options.method || "GET",
        headers: options.body ? {{"Content-Type": "application/json"}} : {{}},
        credentials: "same-origin",
        body: options.body ? JSON.stringify(options.body) : undefined
      }});
      const payload = await response.json().catch(() => ({{error: "LOCAL_RESPONSE_INVALID"}}));
      if (!response.ok) {{
        throw new Error(payload.error || "LOCAL_REQUEST_FAILED");
      }}
      return payload;
    }}

    function idempotencyKey(reviewId, action) {{
      const randomPart = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-" + Math.random();
      return `admin-ui:${{reviewId}}:${{action}}:${{randomPart}}`;
    }}

    function metric(label, value, tone, view, caption) {{
      const badge = tone ? `<span class="badge ${{tone}}">${{tone === "ok" ? "正常" : "需处理"}}</span>` : "";
      return `<div class="metric"><button class="metric-button" data-view-link="${{escapeHTML(view)}}" aria-label="查看${{escapeHTML(label)}}详情"><div class="metric-top"><span class="label">${{escapeHTML(label)}}</span>${{badge}}<span class="metric-arrow" aria-hidden="true">→</span></div><div class="metric-bottom"><div class="value">${{escapeHTML(value)}}</div><div class="metric-caption">${{escapeHTML(caption)}}</div></div></button></div>`;
    }}

    function setConnection(label, tone) {{
      const node = document.getElementById("connection");
      node.className = "connection" + (tone ? " " + tone : "");
      document.getElementById("connection-label").textContent = label;
    }}

    function renderPriority(payload) {{
      const counts = payload.counts || {{}};
      const tasks = [];
      if (counts.pending_reviews) {{
        tasks.push(["有待审核的候选记忆", `当前有 ${{counts.pending_reviews}} 条等待人工判断。`, "审核", "reviews", "warn"]);
      }}
      if (counts.retryable_events) {{
        tasks.push(["有事件等待重试", `当前有 ${{counts.retryable_events}} 条事件会按原有退避策略继续投递。`, "查看运行", "runtime", "warn"]);
      }}
      if (counts.unresolved_dead_letters) {{
        tasks.push(["有未处理的死信", `当前有 ${{counts.unresolved_dead_letters}} 条事件需要根据审计和固定回执判断。`, "查看运行", "runtime", "danger"]);
      }}
      if (!tasks.length) {{
        tasks.push(["当前没有待处理事项", "审核、重试和死信均为空。可以查看近期活动，或按关键词检索记忆。", "查看活动", "activity", "ok"]);
      }}
      document.getElementById("priority-badge").className = "badge " + (tasks.some(item => item[4] === "danger") ? "danger" : tasks.some(item => item[4] === "warn") ? "warn" : "ok");
      document.getElementById("priority-badge").textContent = tasks.some(item => item[4] !== "ok") ? "需要关注" : "状态平稳";
      document.getElementById("priority-list").innerHTML = tasks.map(item => `
        <div class="priority-row">
          <div><div class="row-title">${{escapeHTML(item[0])}}</div><div class="row-copy">${{escapeHTML(item[1])}}</div></div>
          <button data-view-link="${{item[3]}}">${{escapeHTML(item[2])}}</button>
        </div>`).join("");
    }}

    function renderDelivery(payload) {{
      const counts = payload.counts || {{}};
      const rows = [
        ["等待重试", counts.retryable_events || 0, "事件会按既有退避策略继续处理，不会由此页面自动重放。"],
        ["未处理死信", counts.unresolved_dead_letters || 0, "需要先核对回执和审计；此页面不提供删除或批量清理。"],
        ["活跃设备", counts.active_devices || 0, "已登记设备与 Agent 的状态可在“设备与权限”查看。"]
      ];
      document.getElementById("delivery-summary").innerHTML = `<div class="activity-list">${{rows.map(item => `
        <div class="activity-row"><div><div class="row-title">${{escapeHTML(item[0])}}</div><div class="row-copy">${{escapeHTML(item[2])}}</div></div><span class="badge">${{escapeHTML(item[1])}}</span></div>`).join("")}}</div>`;
    }}

    function renderOverview(payload) {{
      state.overview = payload;
      const counts = payload.counts || {{}};
      document.getElementById("metrics").innerHTML = [
        metric("待审核", counts.pending_reviews || 0, counts.pending_reviews ? "warn" : "ok", "reviews", "查看候选与冲突"),
        metric("待重试", counts.retryable_events || 0, counts.retryable_events ? "warn" : "ok", "runtime", "查看同步投递状态"),
        metric("未处理死信", counts.unresolved_dead_letters || 0, counts.unresolved_dead_letters ? "danger" : "ok", "runtime", "核对错误与事件引用"),
        metric("活跃设备", counts.active_devices || 0, null, "devices", "查看设备与授权范围")
      ].join("");
      renderPriority(payload);
      renderDelivery(payload);
    }}

    function renderHealth(payload) {{
      const badge = document.getElementById("health-badge");
      badge.className = "badge " + (payload.ok ? "ok" : "danger");
      badge.textContent = payload.ok ? "正常" : "需要处理";
      const problems = payload.problems && payload.problems.length
        ? payload.problems.map(item => `<span class="badge danger">${{escapeHTML(item)}}</span>`).join(" ")
        : `<span class="badge ok">无异常</span>`;
      document.getElementById("health-panel").innerHTML = `
        <table>
          <tbody>
            <tr><th>检查时间</th><td>${{formatTime(payload.checked_at)}}</td></tr>
            <tr><th>Worker 心跳</th><td>${{formatTime(payload.worker_heartbeat_at)}}</td></tr>
            <tr><th>心跳延迟</th><td>${{escapeHTML(payload.worker_heartbeat_age_seconds ?? "-")}} 秒</td></tr>
            <tr><th>问题</th><td>${{problems}}</td></tr>
          </tbody>
        </table>`;
      setConnection(payload.ok ? "本机连接正常" : "需要处理", payload.ok ? "ok" : "danger");
    }}

    function renderReviews(payload) {{
      state.reviews = payload.reviews || [];
      const root = document.getElementById("review-list");
      if (!state.reviews.length) {{
        root.innerHTML = `<div class="panel"><div class="empty">当前没有待审核候选。</div></div>`;
        return;
      }}
      root.innerHTML = state.reviews.map((item, index) => {{
        const conflicts = (item.conflicts || []).map(conflict => `
          <tr>
            <td>${{code(conflict.backend_ref)}}</td>
            <td>${{escapeHTML(conflict.evidence || "-")}}</td>
            <td>${{escapeHTML(conflict.confidence ?? "-")}}</td>
            <td><button data-action="supersede" data-index="${{index}}" data-target="${{escapeHTML(conflict.backend_ref)}}">取代这条</button></td>
          </tr>
        `).join("");
        const conflictTable = conflicts ? `
          <div class="panel">
            <div class="panel-head"><div class="panel-title">可能冲突</div></div>
            <div class="panel-body">
              <table><thead><tr><th>引用</th><th>证据</th><th>置信度</th><th></th></tr></thead><tbody>${{conflicts}}</tbody></table>
            </div>
          </div>` : "";
        return `
          <article class="review">
            <div class="review-head">
              <div>
                <div class="panel-title">${{escapeHTML(item.kind || "note")}} · ${{escapeHTML(item.scope || "-")}}</div>
                <div class="subtle">${{code(item.review_id)}} · revision ${{escapeHTML(item.revision)}}</div>
              </div>
              <span class="badge ${{item.instruction_like ? "warn" : ""}}">${{item.instruction_like ? "疑似指令" : "普通候选"}}</span>
            </div>
            <div class="review-content">${{escapeHTML(item.content || "")}}</div>
            <label class="label" for="edit-${{index}}">编辑后确认</label>
            <textarea id="edit-${{index}}" data-edit-index="${{index}}">${{escapeHTML(item.content || "")}}</textarea>
            <div class="actions">
              <button class="primary" data-action="confirm" data-index="${{index}}">确认原文</button>
              <button data-action="confirm_edit" data-index="${{index}}">按编辑确认</button>
              <button data-action="retain_both" data-index="${{index}}">保留双方</button>
              <button class="danger" data-action="reject" data-index="${{index}}">拒绝</button>
              <button data-action="archive" data-index="${{index}}">归档</button>
              ${{item.instruction_like ? `<label class="badge"><input type="checkbox" data-approve-index="${{index}}"> 已确认按数据处理</label>` : ""}}
            </div>
            ${{conflictTable}}
          </article>
        `;
      }}).join("");
    }}

    const resultLabels = {{
      "confirmed": "已确认",
      "confirmed_edit": "已确认编辑", 
      "confirmed_superseding": "已取代",
      "CANDIDATE_CREATED": "候选已创建",
      "PENDING": "等待中",
      "APPLIED": "已应用",
      "REJECTED": "已拒绝",
      "ARCHIVED": "已归档",
      "retained": "已保留双方",
      "reverted": "已撤销",
      "ok": "正常",
      "warn": "需关注",
      "danger": "异常",
      "paired": "已配对",
      "queued": "已排队",
      "rotated": "已轮换",
      "created": "已创建",
      "paired": "已配对",
    }};
    function resultLabel(code) {{
      return resultLabels[code] || String(code || "-");
    }}

    function stateBadge(value) {{
      const normalized = String(value || "unknown").toLowerCase();
      const tone = ["active", "online", "bound", "ready", "healthy", "success", "confirmed", "applied", "accepted", "completed"].includes(normalized)
        ? "ok"
        : ["revoked", "disabled", "offline", "blocked", "error"].includes(normalized) ? "danger" : "warn";
      return `<span class="badge ${{tone}}">${{escapeHTML(value || "未知")}}</span>`;
    }}

    function formatTime(raw) {{
      if (!raw) return "暂无记录";
      try {{
        const d = new Date(raw);
        if (isNaN(d.getTime())) return String(raw);
        const pad = (n) => String(n).padStart(2, "0");
        return `${{d.getFullYear()}}-${{pad(d.getMonth() + 1)}}-${{pad(d.getDate())}} ${{pad(d.getHours())}}:${{pad(d.getMinutes())}}:${{pad(d.getSeconds())}}`;
      }} catch {{
        return String(raw);
      }}
    }}

    function deviceOnline(lastSeenAt) {{
      if (!lastSeenAt) return false;
      return (Date.now() - new Date(lastSeenAt).getTime()) < 15 * 60 * 1000;
    }}

    function renderDevices(payload) {{
      state.devices = payload.devices || [];
      state.capabilityCatalog = payload.capability_catalog || [];
      const rows = state.devices.map((item, index) => {{
        const deviceName = item.device_name || item.device_id || "未命名设备";
        const agentName = item.agent_name || item.agent_installation_id || "未登记 Agent";
        const deviceStatus = item.device_status || item.status || "未知";
        const agentStatus = item.agent_status || "未知";
        const bindingStatus = item.binding_status || "未返回绑定状态";
        const capabilities = (item.capabilities || []).map(capability => `<span class="badge">${{escapeHTML(capability)}}</span>`).join("") || `<span class="subtle">未返回能力</span>`;
        const catalog = [...new Set([...state.capabilityCatalog, ...(item.capabilities || [])])].sort();
        const permissionOptions = catalog.map(capability => {{
          const checked = (item.capabilities || []).includes(capability);
          const knownCapability = state.capabilityCatalog.includes(capability);
          const lockManage = Boolean(item.is_current_agent && capability === "memory.manage");
          const locked = lockManage || !knownCapability;
          const note = lockManage ? "当前管理权限" : !knownCapability ? "扩展能力（只读）" : "";
          return `<label class="permission-option"><input type="checkbox" data-capability-index="${{index}}" value="${{escapeHTML(capability)}}" ${{checked ? "checked" : ""}} ${{locked ? "disabled" : ""}}> <code>${{escapeHTML(capability)}}</code>${{note ? `<span class="badge">${{note}}</span>` : ""}}</label>`;
        }}).join("");
        const lastSeen = formatTime(item.device_last_seen_at || item.updated_at || item.created_at);
        const bindingUpdated = formatTime(item.binding_updated_at);
        const online = deviceOnline(item.device_last_seen_at);
        const onlineDot = online
          ? `<span class="online-dot" title="15 分钟内活跃"></span>`
          : `<span class="online-dot off" title="超过 15 分钟未出现"></span>`;
        const identifiers = [
          item.device_id ? `设备：${{code(item.device_id)}}` : "",
          item.agent_installation_id ? `Agent：${{code(item.agent_installation_id)}}` : "",
          item.device_auth_epoch !== undefined && item.device_auth_epoch !== null ? `设备认证版本：${{escapeHTML(item.device_auth_epoch)}}` : "",
          item.agent_auth_epoch !== undefined && item.agent_auth_epoch !== null ? `Agent 认证版本：${{escapeHTML(item.agent_auth_epoch)}}` : ""
        ].filter(Boolean).join("<br>");
        return `
          <tr data-device-row="${{index}}">
            <td><div class="cell-title">${{onlineDot}}${{escapeHTML(deviceName)}}</div><div class="cell-meta"><span class="badge">${{escapeHTML(item.device_type || "device")}}</span></div><details class="record-details"><summary>查看技术标识</summary>${{identifiers}}</details></td>
            <td><div class="cell-title">${{escapeHTML(agentName)}}</div><div class="cell-meta"><span class="badge">${{escapeHTML(item.agent_type || "agent")}}</span></div></td>
            <td><div class="cell-stack"><div><span class="label">设备</span><span class="badge ${{online ? "ok" : "offline"}}">${{online ? "已连线" : "未连线"}}</span></div><div><span class="label">Agent</span> ${{stateBadge(agentStatus)}}</div><div><span class="label">绑定</span> ${{stateBadge(bindingStatus)}}</div></div></td>
            <td><div class="capability-list">${{capabilities}}</div></td>
            <td><div class="cell-stack"><div><span class="label">最近出现</span><div class="cell-copy">${{escapeHTML(lastSeen)}}</div></div><div><span class="label">绑定更新</span><div class="cell-copy">${{escapeHTML(bindingUpdated)}}</div></div></div></td>
            <td>
              <details class="device-manage">
                <summary>管理</summary>
                <div class="device-editor">
                  <div>
                    <div class="cell-title">当前工作区权限</div>
                    <div class="row-copy">保存后只改变 ${{escapeHTML(state.workspaceId)}} 中这个 Agent 的能力。</div>
                  </div>
                  <div class="permission-grid">${{permissionOptions}}</div>
                  <div class="actions"><button class="primary" data-device-action="save-binding" data-index="${{index}}" ${{bindingStatus !== "active" && bindingStatus !== "bound" ? "disabled" : ""}}>保存权限</button></div>
                  <div class="danger-zone">
                    <div class="cell-title">撤销访问</div>
                    <div class="row-copy">撤销 Agent 会影响它的所有工作区；撤销设备会同时停用该设备上的 Agent 和刷新凭据。记录会保留，不会删除数据。</div>
                    <div class="actions">
                      <button class="danger" data-device-action="revoke-agent" data-index="${{index}}" ${{item.is_current_agent || agentStatus !== "active" ? "disabled" : ""}}>撤销 Agent</button>
                      <button class="danger" data-device-action="revoke-device" data-index="${{index}}" ${{item.is_current_device || deviceStatus !== "active" ? "disabled" : ""}}>撤销设备</button>
                    </div>
                    ${{item.is_current_agent || item.is_current_device ? '<div class="row-copy">当前管理端不能撤销自身，也不能移除自身的 memory.manage。</div>' : ""}}
                  </div>
                </div>
              </details>
            </td>
          </tr>`;
      }}).join("");
      document.getElementById("device-list").innerHTML = rows
        ? `<table><thead><tr><th>设备</th><th>Agent</th><th>状态与绑定</th><th>授权能力</th><th>最近状态</th><th>操作</th></tr></thead><tbody>${{rows}}</tbody></table>`
        : `<div class="empty">没有可显示的设备记录。</div>`;
    }}

    function auditTone(value) {{
      const normalized = String(value || "").toLowerCase();
      if (["revoked", "rejected", "error", "failed", "blocked"].some(token => normalized.includes(token))) return "danger";
      if (["pending", "candidate", "retry", "queued"].some(token => normalized.includes(token))) return "warn";
      return "ok";
    }}

    function renderAuditTable() {{
      const query = document.getElementById("activity-query").value.trim().toLowerCase();
      const tone = document.getElementById("activity-result").value;
      const pageSize = parseInt(document.getElementById("activity-page-size").value) || 20;
      state.activityPage = state.activityPage || 1;
      const filtered = state.audit.filter(item => {{
        if (item.action === "auth.token.refresh") return false;
        const haystack = [
          item.action,
          auditActionNames[item.action],
          item.result_code,
          item.target_ref,
          item.source_device_name,
          item.device_id,
          item.source_agent_name,
          item.agent_installation_id,
          item.source_device_type,
          item.source_agent_type
        ].filter(Boolean).join(" ").toLowerCase();
        return (!query || haystack.includes(query)) && (!tone || auditTone(item.result_code) === tone);
      }});
      const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
      if (state.activityPage > totalPages) state.activityPage = totalPages;
      const start = (state.activityPage - 1) * pageSize;
      const page = filtered.slice(start, start + pageSize);
      const rows = page.map((item, idx) => {{
        const deviceName = item.source_device_name || item.device_id || "未识别设备";
        const agentName = item.source_agent_name || item.agent_installation_id || item.actor_id || "系统任务";
        const deviceMeta = [item.source_device_type, item.source_device_status].filter(Boolean).join(" · ");
        const agentMeta = [item.source_agent_type, item.source_agent_status].filter(Boolean).join(" · ");
        const hasTarget = item.target_ref && item.target_ref.startsWith("gbrain:fact:");
        const clickHandler = hasTarget ? ` onclick="viewMemoryDetail('${{escapeHTML(item.target_ref)}}')" style="cursor:pointer;color:var(--accent)"` : "";
        return `
        <tr>
          <td><div class="cell-title">${{escapeHTML(auditActionNames[item.action] || item.action || "管理操作")}}</div><div class="cell-meta">执行者：${{escapeHTML(item.actor_id || item.actor_type || "-")}}</div><details class="record-details"><summary>查看操作代码</summary>${{code(item.action)}}</details></td>
          <td>${{stateBadge(resultLabel(item.result_code))}}</td>
          <td class="source-cell"><div class="source-summary"><span class="cell-title">${{escapeHTML(deviceName)}}</span>${{deviceMeta ? `<span class="badge">${{escapeHTML(deviceMeta)}}</span>` : ""}}</div><div class="cell-copy">${{escapeHTML(agentName)}}</div>${{agentMeta ? `<div class="cell-meta">${{escapeHTML(agentMeta)}}</div>` : ""}}<details class="record-details"><summary>查看来源标识</summary>${{item.device_id ? `设备：${{code(item.device_id)}}` : ""}}${{item.agent_installation_id ? `<br>Agent：${{code(item.agent_installation_id)}}` : ""}}</details></td>
          <td><div class="cell-copy"${{clickHandler}}>${{hasTarget ? "📋 " : ""}}${{escapeHTML(item.target_ref || "未提供目标引用")}}</div></td>
          <td><div class="cell-copy">${{formatTime(item.created_at)}}</div><div class="cell-meta">${{code(item.trace_id)}}</div></td>
        </tr>
      `;
      }}).join("");
      document.getElementById("audit-list").innerHTML = rows
        ? `<table><thead><tr><th>操作</th><th>结果</th><th>来源设备与 Agent</th><th>目标</th><th>时间</th></tr></thead><tbody>${{rows}}</tbody></table>`
        : `<div class="empty">没有符合当前筛选条件的活动记录。</div>`;
      document.getElementById("activity-count").textContent = `${{filtered.length}} 条记录`;
      document.getElementById("activity-page-info").textContent = `第 ${{state.activityPage}} / ${{totalPages}} 页`;
      document.getElementById("activity-prev").disabled = state.activityPage <= 1;
      document.getElementById("activity-next").disabled = state.activityPage >= totalPages;
    }}

    function viewMemoryDetail(backendRef) {{
      if (!backendRef) return;
      const root = document.getElementById("memory-results");
      const cached = (state.allMemories || []).filter(m => m.backend_ref === backendRef || m.memory_id === backendRef);
      showView("memories");
      if (cached.length) {{
        state.allMemories = [cached[0]];
        state.memoryQuery = backendRef;
        state.memoryPage = 1;
        renderMemoryPage();
        document.getElementById("memory-results").scrollIntoView({{behavior: "smooth"}});
        return;
      }}
      root.className = "empty";
      root.textContent = "正在加载记忆详情…";
      api("/api/memories?q=" + encodeURIComponent(backendRef)).then(payload => {{
        const memories = payload.memories || [];
        if (memories.length) {{
          state.allMemories = memories;
          state.memoryQuery = backendRef;
          state.memoryPage = 1;
          renderMemoryPage();
        }} else {{
          root.className = "empty";
          root.textContent = "未找到关联记忆 " + backendRef + "。该引用可能已被归档或尚未通过审核。";
          document.getElementById("memory-result-count").textContent = "0 条结果";
        }}
      }}).catch(err => {{
        root.className = "empty";
        root.textContent = "加载失败：" + (err.message || "未知错误");
      }});
    }}

    function renderAudit(payload) {{
      state.audit = payload.entries || [];
      renderAuditTable();
      const nonRefresh = state.audit.filter(item => item.action !== "auth.token.refresh");
      const preview = nonRefresh.slice(0, 5).map(item => `
        <div class="activity-row">
          <div><div class="row-title">${{escapeHTML(auditActionNames[item.action] || item.action || "管理操作")}}</div><div class="row-copy">${{escapeHTML(item.source_device_name || item.device_id || "系统任务")}} · ${{escapeHTML(item.source_agent_name || item.agent_installation_id || item.actor_id || "-")}}</div></div>
          <div class="row-time">${{formatTime(item.created_at)}}</div>
        </div>`).join("");
      document.getElementById("overview-audit-list").innerHTML = preview || `<div class="empty">还没有可展示的近期活动。</div>`;
    }}

    function renderDeadLetters(payload) {{
      const rows = (payload.dead_letters || []).map(item => `
        <tr>
          <td><div class="cell-title">${{escapeHTML(item.error_code || "未分类错误")}}</div><div class="cell-meta">${{escapeHTML(item.error_class || "-")}}</div></td>
          <td><div class="cell-copy">${{formatTime(item.created_at)}}</div></td>
          <td><div class="cell-copy">事件：${{code(item.event_id)}}</div><div class="cell-meta">死信：${{code(item.dead_letter_id)}}</div></td>
        </tr>
      `).join("");
      document.getElementById("dead-letter-list").innerHTML = rows
        ? `<table><thead><tr><th>错误</th><th>进入时间</th><th>事件引用</th></tr></thead><tbody>${{rows}}</tbody></table>`
        : `<div class="empty">当前没有未处理死信。</div>`;
    }}

    function renderMemories(payload, query) {{
      const memories = payload.memories || [];
      const retrieval = payload.retrieval || {{}};
      const badge = document.getElementById("memory-result-count");
      const isBrowse = !query;
      badge.textContent = isBrowse ? `${{memories.length}} 条共享记忆` : `${{memories.length}} 条结果`;
      badge.className = "badge";
      if (!memories.length) {{
        document.getElementById("memory-results").className = "empty";
        document.getElementById("memory-results").textContent = isBrowse
          ? "当前工作区还没有共享记忆。记忆经过写入→审核确认后才会出现在这里。"
          : `没有找到与"${{query}}"匹配的已授权记忆。`;
        return;
      }}
      document.getElementById("memory-results").className = "memory-list";
      document.getElementById("memory-results").innerHTML = memories.map(item => `
        <article class="memory-row">
          <div>
            <div class="row-title">${{escapeHTML(item.kind || item.memory_type || "记忆")}} <span class="badge">${{escapeHTML(item.lifecycle_status || item.status || "active")}}</span></div>
            <div class="memory-content">${{escapeHTML(item.content || "")}}</div>
            <div class="memory-meta">
              <span>${{escapeHTML(item.scope || "-")}}</span><span>·</span>
              <span>来源：${{escapeHTML((item.source_device_id || item.source_agent_id || "")).replace(/-windows-.*|-fn.*|central-.*/,"").trim() || "记忆中枢"}}</span><span>·</span>
              <span>${{code(item.backend_ref || item.memory_id)}}</span>
              ${{item.superseded_by ? `<span>·</span><span class="badge">已被 ${{code(item.superseded_by)}} 取代</span>` : ""}}
            </div>
          </div>
          <div class="row-time">置信度<br>${{escapeHTML(item.confidence ?? "-")}}</div>
        </article>`).join("");
      if (retrieval.incomplete) {{
        toast("结果受当前检索预算限制，已展示可用部分。");
      }}
    }}

    function errorCopy(code) {{
        const messages = {{
          LOCAL_METHOD_UNSUPPORTED: ["需要更新本机 Sidecar", "当前运行的 Sidecar 还没有加载管理功能。请先完成软件更新，并在维护窗口重新启动 Sidecar；浏览器不会直接访问 Gateway。"],
          GATEWAY_UNAVAILABLE: ["暂时连不上共享服务", "请检查本机 Sidecar、网络和 Gateway 健康状态，然后重新读取。"],
          WORKSPACE_FORBIDDEN: ["当前 Agent 没有该工作区权限", "请核对当前管理 Agent 的工作区授权和 memory.manage 能力。"],
          internal_error: ["部分管理数据暂不可用", "其中一项管理数据暂时无法读取。其他已加载的信息仍可使用；稍后可再次刷新。"]
        }};
        return messages[code] || ["暂时无法读取管理数据", "管理页保留了本机会话和凭据边界。请稍后重新读取，或根据错误码检查 Sidecar 与 Gateway。"];
      }}

      function rejectedCode(result) {{
        return result.status === "rejected" ? String(result.reason && result.reason.message || "LOCAL_REQUEST_FAILED") : "";
      }}

      function renderUnavailable(elementId, code) {{
        document.getElementById(elementId).innerHTML = `<div class="empty">${{escapeHTML(errorCopy(code)[1])}}</div>`;
      }}

    function showLoadError(code) {{
      const message = errorCopy(code);
      document.getElementById("load-error").hidden = false;
      document.getElementById("load-error-title").textContent = message[0];
      document.getElementById("load-error-copy").textContent = message[1];
      document.getElementById("metrics").innerHTML = "";
      document.getElementById("priority-list").innerHTML = "";
      document.getElementById("health-panel").innerHTML = "";
      document.getElementById("overview-audit-list").innerHTML = "";
      setConnection(message[0], "danger");
    }}

    function loadAllMemories() {{
      const root = document.getElementById("memory-results");
      const badge = document.getElementById("memory-result-count");
      root.className = "empty";
      root.textContent = "正在加载共享记忆…";
      badge.textContent = "加载中";
      state.memoryPage = state.memoryPage || 1;
      api("/api/memories").then(payload => {{
        state.allMemories = payload.memories || [];
        state.memoryQuery = null;
        renderMemoryPage();
      }}).catch(error => {{
        root.className = "empty";
        root.textContent = "记忆加载失败：" + (error.message || "未知错误");
        badge.textContent = "暂不可用";
      }});
    }}

    function renderMemoryPage() {{
      const memories = state.allMemories || [];
      const query = state.memoryQuery;
      const isBrowse = !query;
      const pageSize = parseInt(document.getElementById("memory-page-size").value) || 20;
      state.memoryPage = state.memoryPage || 1;
      const total = memories.length;
      const totalPages = Math.max(1, Math.ceil(total / pageSize));
      if (state.memoryPage > totalPages) state.memoryPage = totalPages;
      const start = (state.memoryPage - 1) * pageSize;
      const page = memories.slice(start, start + pageSize);
      const badge = document.getElementById("memory-result-count");
      badge.textContent = isBrowse ? `${{total}} 条共享记忆` : `${{total}} 条结果`;
      badge.className = "badge toolbar-count";
      if (!page.length) {{
        document.getElementById("memory-results").className = "empty";
        document.getElementById("memory-results").textContent = isBrowse
          ? "当前工作区还没有共享记忆。记忆经过写入→审核确认后才会出现在这里。"
          : `没有找到与"${{query}}"匹配的已授权记忆。`;
        document.getElementById("memory-pagination").style.display = "none";
        return;
      }}
      document.getElementById("memory-results").className = "memory-list";
      document.getElementById("memory-results").innerHTML = page.map(item => `
        <article class="memory-row">
          <div>
            <div class="row-title">${{escapeHTML(item.kind || item.memory_type || "记忆")}} <span class="badge">${{escapeHTML(item.lifecycle_status || item.status || "active")}}</span></div>
            <div class="memory-content">${{escapeHTML(item.content || "")}}</div>
            <div class="memory-meta">
              <span>${{escapeHTML(item.scope || "-")}}</span><span>·</span>
              <span>来源：${{escapeHTML((item.source_device_id || item.source_agent_id || "").replace(/-windows-.*|-fn.*|central-.*/,"").trim() || "记忆中枢")}}</span><span>·</span>
              <span>${{code(item.backend_ref || item.memory_id)}}</span>
              ${{item.superseded_by ? `<span>·</span><span class="badge">已被 ${{code(item.superseded_by)}} 取代</span>` : ""}}
            </div>
          </div>
          <div class="row-time">置信度<br>${{escapeHTML(item.confidence ?? "-")}}</div>
        </article>`).join("");
      document.getElementById("memory-page-info").textContent = `第 ${{state.memoryPage}} / ${{totalPages}} 页`;
      document.getElementById("memory-prev").disabled = state.memoryPage <= 1;
      document.getElementById("memory-next").disabled = state.memoryPage >= totalPages;
      document.getElementById("memory-pagination").style.display = "flex";
    }}

    function searchMemories() {{
      const input = document.getElementById("memory-query");
      const query = input.value.trim();
      const root = document.getElementById("memory-results");
      const badge = document.getElementById("memory-result-count");
      if (!query || query.length < 2) {{
        loadAllMemories();
        return;
      }}
      root.className = "empty";
      root.textContent = "正在检索已授权记忆…";
      badge.textContent = "检索中";
      api("/api/memories?q=" + encodeURIComponent(query)).then(payload => {{
        state.allMemories = payload.memories || [];
        state.memoryQuery = query;
        state.memoryPage = 1;
        renderMemoryPage();
      }}).catch(error => {{
        root.className = "empty";
        root.textContent = error.message || "检索失败";
        badge.textContent = "暂不可用";
      }});
    }}

    async function refreshAll() {{
      const refreshButton = document.getElementById("refresh");
      refreshButton.disabled = true;
      refreshButton.dataset.loading = "true";
      refreshButton.textContent = "刷新中";
      document.getElementById("load-error").hidden = true;
      document.getElementById("metrics").innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
      document.getElementById("priority-list").innerHTML = `<div class="skeleton"></div><div class="skeleton"></div>`;
      setConnection("正在读取状态", "");
      try {{
        const [overviewResult, healthResult, reviewsResult, devicesResult, auditResult, deadLettersResult] = await Promise.allSettled([
          api("/api/overview"),
          api("/api/health"),
          api("/api/reviews"),
          api("/api/devices"),
          api("/api/audit"),
          api("/api/dead-letters")
        ]);
        if (overviewResult.status !== "fulfilled") throw overviewResult.reason;
        if (healthResult.status !== "fulfilled") throw healthResult.reason;
        const overview = overviewResult.value;
        const health = healthResult.value;
        renderOverview(overview);
        renderHealth(health);
        if (reviewsResult.status === "fulfilled") {{
          renderReviews(reviewsResult.value);
        }} else {{
          renderUnavailable("review-list", rejectedCode(reviewsResult));
        }}
        if (devicesResult.status === "fulfilled") {{
          renderDevices(devicesResult.value);
        }} else {{
          renderUnavailable("device-list", rejectedCode(devicesResult));
        }}
        if (auditResult.status === "fulfilled") {{
          renderAudit(auditResult.value);
        }} else {{
          const code = rejectedCode(auditResult);
          state.audit = [];
          renderUnavailable("audit-list", code);
          renderUnavailable("overview-audit-list", code);
        }}
        if (deadLettersResult.status === "fulfilled") {{
          renderDeadLetters(deadLettersResult.value);
        }} else {{
          renderUnavailable("dead-letter-list", rejectedCode(deadLettersResult));
        }}
        const optionalFailures = [reviewsResult, devicesResult, auditResult, deadLettersResult]
          .filter(result => result.status !== "fulfilled");
        if (optionalFailures.length) {{
          toast("部分管理信息暂时未加载，可稍后刷新。");
        }}
      }} catch (error) {{
        showLoadError(error.message);
        toast(error.message);
      }} finally {{
        refreshButton.disabled = false;
        refreshButton.dataset.loading = "false";
        refreshButton.textContent = "刷新";
      }}
    }}

    function showView(view) {{
      if (!labels[view] || !document.getElementById(view)) return;
      document.querySelectorAll(".nav-button").forEach(item => item.removeAttribute("aria-current"));
      const button = document.querySelector(`.nav-button[data-view="${{view}}"]`);
      if (button) button.setAttribute("aria-current", "page");
      document.querySelectorAll(".view").forEach(item => item.classList.remove("active"));
      document.getElementById(view).classList.add("active");
      const title = document.getElementById("page-title");
      title.textContent = labels[view][0];
      document.getElementById("page-subtitle").textContent = labels[view][1];
      title.focus({{preventScroll: true}});
      if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {{
        window.scrollTo({{top: 0, behavior: "smooth"}});
      }}
      if (view === "memories") loadAllMemories();
      if (view === "graph") loadGraph();
    }}

    function loadAllMemories() {{
      const root = document.getElementById("memory-results");
      const badge = document.getElementById("memory-result-count");
      root.className = "empty";
      root.textContent = "正在加载共享记忆…";
      badge.textContent = "加载中";
      try {{
        api("/api/memories").then(payload => renderMemories(payload, null));
      }} catch (error) {{
        root.className = "empty";
        root.textContent = "记忆加载失败：" + (error.message || "未知错误");
        badge.textContent = "暂不可用";
      }}
    }}

    function loadGraph() {{
      const root = document.getElementById("graph-container");
      root.innerHTML = '<div class="skeleton" style="height:500px"></div>';
      api("/api/memory-graph").then(payload => renderGraph(payload))
        .catch(err => {{ root.innerHTML = `<div class="empty">图谱数据暂不可用：${{err.message}}</div>`; }});
    }}

    function renderGraph(payload) {{
      const nodes = payload.nodes || [];
      const edges = payload.edges || [];
      const root = document.getElementById("graph-container");
      if (!nodes.length) {{
        root.innerHTML = "<div class='empty'>当前还没有可展示的内存关系数据。</div>";
        return;
      }}
      const groups = {{memory: "#2f6fed", device: "#b45309", agent: "#7c3aed"}};
      const byId = {{}};
      nodes.forEach(n => {{ n.x = null; n.y = null; byId[n.id] = n; }});
      
      root.innerHTML = `<div style="padding:1rem;font-size:0.85rem;color:var(--subtle)">
        节点 ${{nodes.length}} · 连线 ${{edges.length}}
        <span style="margin-left:1rem">${{Object.entries(groups).map(([k,v]) => `<span style="color:${{v}}">■</span> ${{k}}`).join(" &nbsp; ")}}</span>
      </div><canvas id="graph-canvas" style="width:100%;height:550px;display:block"></canvas>`;
      
      const canvas = document.getElementById("graph-canvas");
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      const W = canvas.parentElement.clientWidth;
      canvas.width = W * 2; canvas.height = 1100;
      canvas.style.width = W + "px"; canvas.style.height = "550px";
      ctx.scale(2, 2);
      
      const cx = W / 2, cy = 275, rx = W * 0.38, ry = 220;
      nodes.forEach((n, i) => {{
        const angle = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
        n.x = cx + rx * Math.cos(angle);
        n.y = cy + ry * Math.sin(angle);
      }});
      
      edges.forEach(e => {{
        const from = byId[e.from], to = byId[e.to];
        if (!from || !to) return;
        ctx.beginPath();
        ctx.strokeStyle = e.dashes ? "#b45309" : "rgba(47,111,237,0.3)";
        ctx.lineWidth = 1;
        if (e.dashes) ctx.setLineDash([4, 3]);
        ctx.moveTo(from.x, from.y);
        ctx.lineTo(to.x, to.y);
        ctx.stroke();
        ctx.setLineDash([]);
      }});
      
      nodes.forEach(n => {{
        const color = n.status === "archived" ? "#999" : (groups[n.group] || "#666");
        ctx.beginPath();
        ctx.arc(n.x, n.y, 18, 0, 2 * Math.PI);
        ctx.fillStyle = n.status === "archived" ? "rgba(153,153,153,0.15)" : (color + "18");
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.font = "11px -apple-system, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(n.label.slice(0, 12), n.x, n.y + 32);
      }});
    }}

    function resolveReview(index, action, targetRef) {{
      const item = state.reviews[index];
      if (!item) return;
      askConfirmation(action, item, targetRef, async () => {{
        try {{
          const editNode = document.querySelector(`[data-edit-index="${{index}}"]`);
          const approveNode = document.querySelector(`[data-approve-index="${{index}}"]`);
          const payload = {{
            review_id: item.review_id,
            expected_revision: item.revision,
            action,
            idempotency_key: idempotencyKey(item.review_id, action),
            confirmed_by_user: true,
            approve_instruction_like: Boolean(approveNode && approveNode.checked)
          }};
          if (action === "confirm_edit") {{
            payload.content = editNode ? editNode.value : item.content;
            payload.metadata = item.metadata || {{}};
          }}
          if (targetRef) {{
            payload.target_ref = targetRef;
          }}
          const result = await api("/api/reviews/resolve", {{method: "POST", body: payload}});
          state.latestOperation = result.operation_id ? result : null;
          toast(result.status ? `操作已返回：${{result.status}}` : "操作已提交");
          await refreshAll();
        }} catch (error) {{
          toast(error.message);
        }}
      }});
    }}

    function manageDevice(index, action) {{
      const item = state.devices[index];
      if (!item) return;
      const deviceName = item.device_name || item.device_id;
      const agentName = item.agent_name || item.agent_installation_id;
      let title;
      let message;
      let acceptLabel;
      let dangerous = false;
      let path;
      let payload;

      if (action === "save-binding") {{
        const capabilities = [...document.querySelectorAll(`[data-capability-index="${{index}}"]`)]
          .filter(input => input.checked)
          .map(input => input.value)
          .sort();
        if (!capabilities.length) {{
          toast("至少保留一项工作区能力。");
          return;
        }}
        title = `保存 ${{agentName}} 的权限？`;
        message = `只会更新工作区 ${{state.workspaceId}} 的能力。若页面数据已变化，服务端会拒绝覆盖并要求刷新。`;
        acceptLabel = "保存权限";
        path = "/api/devices/binding";
        payload = {{
          target_agent_installation_id: item.agent_installation_id,
          expected_capabilities: item.capabilities || [],
          capabilities,
          idempotency_key: idempotencyKey(item.agent_installation_id, "capabilities"),
          confirmed_by_user: true
        }};
      }} else if (action === "revoke-agent") {{
        title = `撤销 ${{agentName}}？`;
        message = `这个 Agent 将立即失去所有工作区的访问权，需要重新登记才能恢复。设备 ${{deviceName}} 上的其他 Agent 不受影响，历史记录不会删除。`;
        acceptLabel = "撤销 Agent";
        dangerous = true;
        path = "/api/devices/revoke-agent";
        payload = {{
          target_agent_installation_id: item.agent_installation_id,
          expected_auth_epoch: item.agent_auth_epoch,
          idempotency_key: idempotencyKey(item.agent_installation_id, "revoke-agent"),
          confirmed_by_user: true
        }};
      }} else if (action === "revoke-device") {{
        title = `撤销设备 ${{deviceName}}？`;
        message = "该设备、设备上的所有 Agent 和刷新凭据都会立即失效，需要重新配对才能恢复。历史记忆和审计记录不会删除。";
        acceptLabel = "撤销设备";
        dangerous = true;
        path = "/api/devices/revoke-device";
        payload = {{
          target_device_id: item.device_id,
          expected_auth_epoch: item.device_auth_epoch,
          idempotency_key: idempotencyKey(item.device_id, "revoke-device"),
          confirmed_by_user: true
        }};
      }} else {{
        return;
      }}

      askAdminConfirmation(title, message, acceptLabel, dangerous, async () => {{
        try {{
          const result = await api(path, {{method: "POST", body: payload}});
          toast(result.status === "unchanged" ? "权限没有变化。" : "操作已完成并写入审计。" );
          await refreshAll();
        }} catch (error) {{
          toast(error.message === "ADMIN_STATE_CHANGED" ? "设备状态已经变化，请刷新后重试。" : error.message);
        }}
      }});
    }}

    document.addEventListener("click", event => {{
      const nav = event.target.closest(".nav-button");
      if (nav) {{
        showView(nav.dataset.view);
        return;
      }}
      const viewLink = event.target.closest("[data-view-link]");
      if (viewLink) {{
        showView(viewLink.dataset.viewLink);
        return;
      }}
      const actionButton = event.target.closest("[data-action]");
      if (actionButton) {{
        resolveReview(Number(actionButton.dataset.index), actionButton.dataset.action, actionButton.dataset.target);
        return;
      }}
      const deviceAction = event.target.closest("[data-device-action]");
      if (deviceAction) {{
        manageDevice(Number(deviceAction.dataset.index), deviceAction.dataset.deviceAction);
      }}
    }});

    document.getElementById("refresh").addEventListener("click", refreshAll);
    document.getElementById("load-error-refresh").addEventListener("click", refreshAll);
    document.getElementById("memory-search-form").addEventListener("submit", event => {{
      event.preventDefault();
      searchMemories();
    }});
    document.getElementById("activity-query").addEventListener("input", renderAuditTable);
    document.getElementById("activity-result").addEventListener("change", renderAuditTable);
    document.getElementById("activity-page-size").addEventListener("change", () => {{ state.activityPage = 1; renderAuditTable(); }});
    document.getElementById("activity-prev").addEventListener("click", () => {{ if (state.activityPage > 1) {{ state.activityPage--; renderAuditTable(); }} }});
    document.getElementById("activity-next").addEventListener("click", () => {{ state.activityPage++; renderAuditTable(); }});
    document.getElementById("memory-page-size").addEventListener("change", () => {{ state.memoryPage = 1; renderMemoryPage(); }});
    document.getElementById("memory-prev").addEventListener("click", () => {{ if (state.memoryPage > 1) {{ state.memoryPage--; renderMemoryPage(); }} }});
    document.getElementById("memory-next").addEventListener("click", () => {{ state.memoryPage++; renderMemoryPage(); }});
    refreshAll();
  </script>
</body>
</html>""".encode("utf-8")


def _unauthorized_page(nonce: str, mount_path: str = "") -> bytes:
    retry_path = html.escape((mount_path or "") + "/", quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>需要授权此浏览器 · Memory Admin</title>
  <style nonce="{nonce}">
    :root {{ color-scheme: light; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ min-height: 100vh; margin: 0; display: grid; place-items: center; padding: 24px; background: oklch(0.975 0.004 260); color: oklch(0.22 0.018 260); }}
    main {{ width: min(560px, 100%); border: 1px solid oklch(0.885 0.01 260); border-radius: 12px; background: white; padding: clamp(24px, 5vw, 40px); box-shadow: 0 12px 32px oklch(0.22 0.018 260 / .08); }}
    .mark {{ width: 32px; height: 32px; display: grid; place-items: center; border-radius: 9px; background: oklch(0.52 0.16 264); color: white; font-weight: 700; }}
    h1 {{ margin: 22px 0 10px; font-size: 26px; line-height: 1.25; letter-spacing: -.025em; }}
    p {{ margin: 0; color: oklch(0.44 0.018 260); line-height: 1.65; }}
    .steps {{ margin: 24px 0; padding: 16px 18px; border: 1px solid oklch(0.885 0.01 260); border-radius: 9px; background: oklch(0.968 0.004 260); line-height: 1.7; }}
    a {{ display: inline-flex; align-items: center; min-height: 40px; padding: 0 13px; border: 1px solid oklch(0.885 0.01 260); border-radius: 7px; color: oklch(0.22 0.018 260); text-decoration: none; }}
    a:hover {{ background: oklch(0.945 0.006 260); }}
    a:focus-visible {{ outline: 2px solid oklch(0.52 0.16 264); outline-offset: 2px; }}
  </style>
</head>
<body>
  <main>
    <div class="mark">M</div>
    <h1>需要授权此浏览器</h1>
    <p>管理入口没有使用固定密码，也不会把 Gateway 凭据交给浏览器。首次使用或授权到期时，需要从可信管理机完成一次浏览器授权。</p>
    <div class="steps">运行项目中的中枢管理页打开脚本。授权完成后，这个浏览器可以在有效期内直接访问固定 HTTPS 地址，不需要每次再运行命令。</div>
    <a href="{retry_path}">我已完成授权，重新检查</a>
  </main>
</body>
</html>""".encode("utf-8")


class _AdminConsoleHandler(BaseHTTPRequestHandler):
    state: AdminConsoleState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = self._mounted_path(parsed.path)
        if path is None:
            self._json({"error": "NOT_FOUND"}, status=404)
            return
        if path == "/":
            params = parse_qs(parsed.query)
            launch_token = (params.get("session") or [""])[0]
            if launch_token:
                session = self.state.session.consume_launch_token(launch_token)
                if session:
                    self._redirect_with_session(session)
                    return
            if not self._authorized():
                nonce = secrets.token_urlsafe(16)
                self._send_bytes(
                    _unauthorized_page(nonce, self.state.mount_path),
                    status=401,
                    content_type="text/html; charset=utf-8",
                    nonce=nonce,
                )
                return
            nonce = secrets.token_urlsafe(16)
            self._send_bytes(
                _html_page(self.state.workspace_id, nonce, self.state.mount_path),
                content_type="text/html; charset=utf-8",
                nonce=nonce,
            )
            return
        if path.startswith("/api/"):
            if not self._authorized():
                self._json({"error": "LOCAL_ADMIN_SESSION_REQUIRED"}, status=401)
                return
            self._handle_api_get(path, parse_qs(parsed.query))
            return
        self._json({"error": "NOT_FOUND"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = self._mounted_path(parsed.path)
        if path is None or not path.startswith("/api/"):
            self._json({"error": "NOT_FOUND"}, status=404)
            return
        if not self._authorized():
            self._json({"error": "LOCAL_ADMIN_SESSION_REQUIRED"}, status=401)
            return
        self._handle_api_post(path)

    def _mounted_path(self, path: str) -> str | None:
        mount_path = self.state.mount_path
        if not mount_path:
            return path
        if path == mount_path:
            return "/"
        if path.startswith(mount_path + "/"):
            return path[len(mount_path) :]
        return None

    def _handle_api_get(self, path: str, query: dict[str, list[str]] | None = None) -> None:
        try:
            sidecar = self.state.sidecar_factory()
            payload = {"workspace_id": self.state.workspace_id}
            if path == "/api/overview":
                self._json(sidecar.admin_overview(payload))
            elif path == "/api/health":
                overview = sidecar.admin_overview(payload)
                self._json(
                    evaluate_overview(
                        overview,
                        max_heartbeat_age_seconds=self.state.max_heartbeat_age_seconds,
                    )
                )
            elif path == "/api/reviews":
                self._json(sidecar.list_reviews(payload | {"limit": 30}))
            elif path == "/api/devices":
                self._json(sidecar.list_admin_devices(payload | {"limit": 50}))
            elif path == "/api/audit":
                self._json(sidecar.list_admin_audit(payload | {"limit": 50}))
            elif path == "/api/dead-letters":
                self._json(sidecar.list_admin_dead_letters(payload | {"limit": 50}))
            elif path == "/api/memories":
                text = str(((query or {}).get("q") or [""])[0]).strip()
                if text and 2 <= len(text) <= 256:
                    self._json(sidecar.search(payload | {"query": text, "limit": 20}))
                else:
                    try:
                        self._json(sidecar.list_memories(payload | {"limit": 200}))
                    except AttributeError:
                        self._json(sidecar.search(payload | {"query": "", "limit": 20}))
            elif path == "/api/memory-graph":
                self._json(sidecar.memory_graph(payload))
            else:
                self._json({"error": "NOT_FOUND"}, status=404)
        except (AdminConsoleError, SidecarDaemonError) as exc:
            self._json({"error": str(exc)}, status=400)

    def _handle_api_post(self, path: str) -> None:
        try:
            payload = self._read_json()
            if not bool(payload.get("confirmed_by_user")):
                raise AdminConsoleError("USER_CONFIRMATION_REQUIRED")
            sidecar = self.state.sidecar_factory()
            if path == "/api/reviews/resolve":
                result = sidecar.resolve_review(self._resolve_payload(payload))
            elif path == "/api/reviews/revert":
                result = sidecar.revert_review(self._revert_payload(payload))
            elif path == "/api/crystals/rebuild":
                result = sidecar.rebuild_crystal({"workspace_id": self.state.workspace_id})
            elif path == "/api/devices/binding":
                result = sidecar.update_admin_binding(self._binding_payload(payload))
            elif path == "/api/devices/revoke-agent":
                result = sidecar.revoke_admin_agent(self._revoke_agent_payload(payload))
            elif path == "/api/devices/revoke-device":
                result = sidecar.revoke_admin_device(self._revoke_device_payload(payload))
            else:
                self._json({"error": "NOT_FOUND"}, status=404)
                return
            self._json(result)
        except (AdminConsoleError, SidecarDaemonError) as exc:
            self._json({"error": str(exc)}, status=400)

    def _resolve_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = _required_text(payload, "action", "REVIEW_ACTION_REQUIRED", 64)
        result: dict[str, Any] = {
            "workspace_id": self.state.workspace_id,
            "review_id": _required_text(payload, "review_id", "REVIEW_ID_REQUIRED", 128),
            "action": action,
            "expected_revision": _required_positive_int(payload, "expected_revision", "EXPECTED_REVISION_REQUIRED"),
            "idempotency_key": _required_text(payload, "idempotency_key", "IDEMPOTENCY_KEY_REQUIRED", 256),
            "confirmed_by_user": True,
        }
        if bool(payload.get("approve_instruction_like")):
            result["approve_instruction_like"] = True
        if action == "confirm_edit":
            result["content"] = _required_text(payload, "content", "CONTENT_REQUIRED", 20_000)
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                raise AdminConsoleError("METADATA_INVALID")
            result["metadata"] = metadata
        if action == "supersede":
            result["target_ref"] = _required_text(payload, "target_ref", "SUPERSEDE_TARGET_REQUIRED", 128)
        return result

    def _revert_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace_id": self.state.workspace_id,
            "review_id": _required_text(payload, "review_id", "REVIEW_ID_REQUIRED", 128),
            "operation_id": _required_text(payload, "operation_id", "OPERATION_ID_REQUIRED", 128),
            "expected_revision": _required_positive_int(payload, "expected_revision", "EXPECTED_REVISION_REQUIRED"),
            "idempotency_key": _required_text(payload, "idempotency_key", "IDEMPOTENCY_KEY_REQUIRED", 256),
            "confirmed_by_user": True,
        }

    def _binding_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace_id": self.state.workspace_id,
            "target_agent_installation_id": _required_text(
                payload,
                "target_agent_installation_id",
                "TARGET_AGENT_INSTALLATION_ID_REQUIRED",
            ),
            "expected_capabilities": _required_capabilities(
                payload,
                "expected_capabilities",
                "EXPECTED_CAPABILITIES_INVALID",
            ),
            "capabilities": _required_capabilities(
                payload,
                "capabilities",
                "WORKSPACE_CAPABILITIES_INVALID",
            ),
            "idempotency_key": _required_text(
                payload,
                "idempotency_key",
                "IDEMPOTENCY_KEY_REQUIRED",
            ),
            "confirmed_by_user": True,
        }

    def _revoke_agent_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace_id": self.state.workspace_id,
            "target_agent_installation_id": _required_text(
                payload,
                "target_agent_installation_id",
                "TARGET_AGENT_INSTALLATION_ID_REQUIRED",
            ),
            "expected_auth_epoch": _required_positive_int(
                payload,
                "expected_auth_epoch",
                "EXPECTED_AUTH_EPOCH_REQUIRED",
            ),
            "idempotency_key": _required_text(
                payload,
                "idempotency_key",
                "IDEMPOTENCY_KEY_REQUIRED",
            ),
            "confirmed_by_user": True,
        }

    def _revoke_device_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace_id": self.state.workspace_id,
            "target_device_id": _required_text(
                payload,
                "target_device_id",
                "TARGET_DEVICE_ID_REQUIRED",
            ),
            "expected_auth_epoch": _required_positive_int(
                payload,
                "expected_auth_epoch",
                "EXPECTED_AUTH_EPOCH_REQUIRED",
            ),
            "idempotency_key": _required_text(
                payload,
                "idempotency_key",
                "IDEMPOTENCY_KEY_REQUIRED",
            ),
            "confirmed_by_user": True,
        }

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise AdminConsoleError("LOCAL_REQUEST_INVALID") from exc
        if not 0 < length <= MAX_ADMIN_BODY_BYTES:
            raise AdminConsoleError("LOCAL_REQUEST_SIZE_INVALID")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise AdminConsoleError("LOCAL_REQUEST_INVALID") from exc
        if not isinstance(payload, dict):
            raise AdminConsoleError("LOCAL_REQUEST_INVALID")
        return payload

    def _authorized(self) -> bool:
        return self.state.session.authorized(self.headers.get("Cookie"))

    def _redirect_with_session(self, session_token: str) -> None:
        self.send_response(303)
        cookie_path = self.state.mount_path or "/"
        self.send_header("Location", f"{cookie_path}/")
        self.send_header("Cache-Control", "no-store")
        secure = "; Secure" if self.state.secure_cookie else ""
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}={session_token}; HttpOnly; SameSite=Strict; "
            f"Path={cookie_path}; Max-Age={self.state.session.max_age_seconds}{secure}",
        )
        self.end_headers()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(body, status=status, content_type="application/json; charset=utf-8")

    def _send_bytes(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str,
        nonce: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if nonce:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; "
                f"style-src 'nonce-{nonce}'; "
                "connect-src 'self'; "
                "img-src 'self' data:; "
                "base-uri 'none'; "
                "form-action 'none'; "
                "frame-ancestors 'none'",
            )
        else:
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def create_admin_console_server(
    *,
    workspace_id: str,
    host: str = "127.0.0.1",
    port: int = 0,
    sidecar_factory: Callable[[], Any] = get_shared_sidecar,
    session: LocalAdminSession | None = None,
    max_heartbeat_age_seconds: int = 90,
    allow_network: bool = False,
    mount_path: str = "",
    secure_cookie: bool = False,
) -> ThreadingHTTPServer:
    is_loopback = host in {"127.0.0.1", "::1", "localhost"}
    if not is_loopback and (not allow_network or host not in {"0.0.0.0", "::"}):
        raise AdminConsoleError("管理控制台只能监听回环地址")
    if not 1024 <= int(port or 0) <= 65535 and int(port or 0) != 0:
        raise AdminConsoleError("PORT_INVALID")
    state = AdminConsoleState(
        workspace_id=_workspace_id(workspace_id),
        session=session or LocalAdminSession(),
        sidecar_factory=sidecar_factory,
        max_heartbeat_age_seconds=max_heartbeat_age_seconds,
        mount_path=_mount_path(mount_path),
        secure_cookie=bool(secure_cookie),
    )
    handler = type(
        "ConfiguredAdminConsoleHandler",
        (_AdminConsoleHandler,),
        {"state": state},
    )
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本机共享记忆管理页")
    parser.add_argument("--workspace", default=os.environ.get("MEMORY_DEFAULT_WORKSPACE"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--max-heartbeat-age-seconds", type=int, default=90)
    parser.add_argument("--allow-network", action="store_true", help="仅供受控反向代理容器使用")
    parser.add_argument("--mount-path", default="", help="反向代理中的固定挂载路径，例如 /admin")
    parser.add_argument("--secure-cookie", action="store_true", help="只在 HTTPS 反向代理后使用")
    parser.add_argument("--public-base-url", help="受控 HTTPS 管理入口，例如 https://memory.example/admin")
    parser.add_argument("--launch-token-file", help="受保护目录中的一次性启动链接文件")
    parser.add_argument("--session-key-file", help="持久浏览器会话签名密钥；中枢模式默认与启动链接同目录")
    parser.add_argument("--session-max-age-days", type=int, default=30, help="中枢浏览器授权有效期，默认 30 天")
    args = parser.parse_args()
    if not 1 <= args.max_heartbeat_age_seconds <= 3600:
        raise SystemExit("MAX_HEARTBEAT_AGE_INVALID")
    mount_path = _mount_path(args.mount_path)
    if bool(args.public_base_url) != bool(args.launch_token_file):
        raise SystemExit("ADMIN_LAUNCH_CONFIGURATION_INVALID")
    if args.public_base_url and (not args.allow_network or not args.secure_cookie):
        raise SystemExit("ADMIN_NETWORK_SECURITY_REQUIRED")
    if not 1 <= args.session_max_age_days <= MAX_PERSISTENT_SESSION_DAYS:
        raise SystemExit("ADMIN_SESSION_MAX_AGE_INVALID")
    if args.public_base_url:
        session_key_file = args.session_key_file or str(
            Path(str(args.launch_token_file)).with_name("session.key")
        )
        try:
            session_secret = _load_or_create_session_secret(session_key_file)
        except AdminConsoleError as exc:
            raise SystemExit(str(exc)) from None
        session = LocalAdminSession(
            session_token=session_secret,
            max_age_seconds=args.session_max_age_days * 86_400,
        )
    else:
        if args.session_key_file:
            raise SystemExit("ADMIN_SESSION_KEY_FILE_UNEXPECTED")
        session = LocalAdminSession()
    try:
        server = create_admin_console_server(
            workspace_id=_workspace_id(args.workspace),
            host=args.host,
            port=args.port,
            session=session,
            max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
            allow_network=args.allow_network,
            mount_path=mount_path,
            secure_cookie=args.secure_cookie,
        )
    except AdminConsoleError as exc:
        raise SystemExit(str(exc)) from None
    if args.public_base_url:
        base_url = _public_base_url(args.public_base_url, mount_path)
        url = f"{base_url}/?session={session.launch_token}"
        _write_launch_url(str(args.launch_token_file), url)
    else:
        url = f"http://{args.host}:{server.server_port}{mount_path}/?session={session.launch_token}"
    print(f"Memory Admin Console listening on http://{args.host}:{server.server_port}", flush=True)
    if args.public_base_url:
        print("Central admin launch link was written to the protected launch file.", flush=True)
    else:
        print(f"Open once: {url}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
