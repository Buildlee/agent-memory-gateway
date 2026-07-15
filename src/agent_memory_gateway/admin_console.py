"""Local-only browser console for Memory Gateway operations."""

from __future__ import annotations

import argparse
import html
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .admin_check import evaluate_overview
from .sidecar_daemon import SidecarDaemonError, get_shared_sidecar


MAX_ADMIN_BODY_BYTES = 131_072
SESSION_COOKIE_NAME = "memory_admin_session"


class AdminConsoleError(RuntimeError):
    """Stable local console error code."""


class LocalAdminSession:
    """One-time launch token exchanged for a local HttpOnly cookie."""

    def __init__(self, launch_token: str | None = None, session_token: str | None = None) -> None:
        self.launch_token = launch_token or secrets.token_urlsafe(32)
        self.session_token = session_token or secrets.token_urlsafe(32)
        self._used = False
        self._lock = threading.Lock()

    def consume_launch_token(self, supplied: str) -> str | None:
        with self._lock:
            if self._used:
                return None
            if not hmac.compare_digest(supplied, self.launch_token):
                return None
            self._used = True
            return self.session_token

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
        return hmac.compare_digest(morsel.value, self.session_token)


@dataclass(frozen=True)
class AdminConsoleState:
    workspace_id: str
    session: LocalAdminSession
    sidecar_factory: Callable[[], Any]
    max_heartbeat_age_seconds: int = 90


def _workspace_id(value: str | None) -> str:
    workspace_id = str(value or os.environ.get("MEMORY_DEFAULT_WORKSPACE") or "").strip()
    if not workspace_id:
        raise AdminConsoleError("WORKSPACE_ID_REQUIRED")
    if len(workspace_id) > 256:
        raise AdminConsoleError("WORKSPACE_ID_INVALID")
    return workspace_id


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


def _html_page(workspace_id: str, nonce: str) -> bytes:
    escaped_workspace = html.escape(workspace_id, quote=True)
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
      padding: 28px clamp(22px, 4vw, 52px) 48px;
    }}
    .content {{ max-width: 1320px; margin: 0 auto; }}
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
    input[type="search"] {{
      background: white;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--ink);
      min-height: 36px;
      min-width: 0;
      padding: 0 11px;
      width: min(520px, 100%);
    }}
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
      th, td {{ padding: 9px 6px; }}
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
<body data-workspace="{escaped_workspace}">
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
        <button class="nav-button" data-view="reviews"><span class="dot"></span>审核</button>
        <button class="nav-button" data-view="devices"><span class="dot"></span>设备与权限</button>
        <button class="nav-button" data-view="runtime"><span class="dot"></span>运行</button>
        <button class="nav-button" data-view="activity"><span class="dot"></span>活动</button>
      </nav>
      <div class="side-note">管理页只在这台电脑上运行。浏览器不会保存 Gateway 凭据。</div>
    </aside>
    <main>
      <div class="content">
        <div class="topbar">
          <div>
            <p class="eyebrow">工作区管理台</p>
            <h1 id="page-title">共享记忆管理</h1>
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
              <p class="overview-copy">这里集中显示当前工作区最需要处理的事情。先看待审核和投递异常，再进入对应页面继续处理。</p>
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
                <div class="panel-head"><div class="panel-title">使用边界</div></div>
                <div class="panel-body"><div class="row-copy">页面只通过本机 Sidecar 请求已授权工作区。不会显示设备公钥、Gateway 凭据、刷新凭据或数据库连接信息。</div></div>
              </div>
            </div>
          </div>
        </section>
        <section id="memories" class="view">
          <div class="panel">
            <div class="panel-head"><div><div class="panel-title">记忆检索</div><div class="subtle">只查询当前工作区内、当前 Agent 已获授权的记忆。</div></div></div>
            <div class="panel-body">
              <form id="memory-search-form" class="memory-search">
                <label class="sr-only" for="memory-query">记忆检索关键词</label>
                <input id="memory-query" type="search" minlength="2" maxlength="256" required placeholder="输入关键词，例如：发布流程">
                <button class="primary" type="submit">搜索记忆</button>
              </form>
            </div>
          </div>
          <div class="panel">
            <div class="panel-head"><div class="panel-title">检索结果</div><span id="memory-result-count" class="badge">等待查询</span></div>
            <div class="panel-body"><div id="memory-results" class="empty">输入至少两个字开始查询。搜索不会修改记忆或同步队列。</div></div>
          </div>
        </section>
        <section id="reviews" class="view">
          <div id="review-list"></div>
        </section>
        <section id="devices" class="view">
          <div class="panel">
            <div class="panel-head"><div><div class="panel-title">设备与权限</div><div class="subtle">查看已登记设备、Agent 与工作区能力，不显示凭据。</div></div></div>
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
            <div class="panel-head"><div><div class="panel-title">近期活动</div><div class="subtle">记录已完成的管理与审核动作，不显示正文或敏感详情。</div></div></div>
            <div class="panel-body" id="audit-list"></div>
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
      reviews: [],
      latestOperation: null,
      overview: null,
      audit: []
    }};

    const actionNames = {{
      confirm: "确认原文",
      confirm_edit: "按编辑确认",
      retain_both: "保留双方",
      supersede: "取代冲突记忆",
      reject: "拒绝候选",
      archive: "归档候选"
    }};

    const labels = {{
      overview: ["共享记忆管理", "当前工作区：" + state.workspaceId],
      memories: ["记忆", "检索当前工作区中已经授权给你的记忆"],
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

    confirmDialog.addEventListener("close", () => {{
      const confirmed = confirmDialog.returnValue === "confirm";
      const callback = pendingConfirmation;
      pendingConfirmation = null;
      if (confirmed && callback) {{
        callback();
      }}
    }});

    async function api(path, options = {{}}) {{
      const response = await fetch(path, {{
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

    function metric(label, value, tone) {{
      const badge = tone ? `<span class="badge ${{tone}}">${{tone === "ok" ? "正常" : "需处理"}}</span>` : "";
      return `<div class="metric"><div class="label">${{escapeHTML(label)}}</div><div class="value">${{escapeHTML(value)}}</div>${{badge}}</div>`;
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
        metric("待审核", counts.pending_reviews || 0, counts.pending_reviews ? "warn" : "ok"),
        metric("待重试", counts.retryable_events || 0, counts.retryable_events ? "warn" : "ok"),
        metric("未处理死信", counts.unresolved_dead_letters || 0, counts.unresolved_dead_letters ? "danger" : "ok"),
        metric("活跃设备", counts.active_devices || 0, null)
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
            <tr><th>检查时间</th><td>${{escapeHTML(payload.checked_at || "-")}}</td></tr>
            <tr><th>Worker 心跳</th><td>${{escapeHTML(payload.worker_heartbeat_at || "-")}}</td></tr>
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

    function renderDevices(payload) {{
      const rows = (payload.devices || []).map(item => `
        <tr>
          <td>${{code(item.device_id)}}</td>
          <td>${{code(item.agent_installation_id)}}</td>
          <td>${{escapeHTML(item.status || "-")}}</td>
          <td>${{escapeHTML((item.capabilities || []).join(", ") || "-")}}</td>
          <td>${{escapeHTML(item.updated_at || item.created_at || "-")}}</td>
        </tr>
      `).join("");
      document.getElementById("device-list").innerHTML = rows
        ? `<table><thead><tr><th>设备</th><th>Agent</th><th>状态</th><th>能力</th><th>更新时间</th></tr></thead><tbody>${{rows}}</tbody></table>`
        : `<div class="empty">没有可显示的设备记录。</div>`;
    }}

    function renderAudit(payload) {{
      state.audit = payload.entries || [];
      const rows = state.audit.map(item => `
        <tr>
          <td>${{escapeHTML(item.created_at || "-")}}</td>
          <td>${{escapeHTML(item.action || "-")}}</td>
          <td>${{escapeHTML(item.result_code || "-")}}</td>
          <td>${{code(item.trace_id)}}</td>
        </tr>
      `).join("");
      document.getElementById("audit-list").innerHTML = rows
        ? `<table><thead><tr><th>时间</th><th>操作</th><th>结果</th><th>Trace</th></tr></thead><tbody>${{rows}}</tbody></table>`
        : `<div class="empty">没有近期审计记录。</div>`;
      const preview = state.audit.slice(0, 5).map(item => `
        <div class="activity-row">
          <div><div class="row-title">${{escapeHTML(item.action || "管理操作")}}</div><div class="row-copy">${{escapeHTML(item.result_code || "-")}}</div></div>
          <div class="row-time">${{escapeHTML(item.created_at || "-")}}</div>
        </div>`).join("");
      document.getElementById("overview-audit-list").innerHTML = preview || `<div class="empty">还没有可展示的近期活动。</div>`;
    }}

    function renderDeadLetters(payload) {{
      const rows = (payload.dead_letters || []).map(item => `
        <tr>
          <td>${{code(item.dead_letter_id)}}</td>
          <td>${{escapeHTML(item.error_code || "-")}}</td>
          <td>${{escapeHTML(item.error_class || "-")}}</td>
          <td>${{escapeHTML(item.created_at || "-")}}</td>
        </tr>
      `).join("");
      document.getElementById("dead-letter-list").innerHTML = rows
        ? `<table><thead><tr><th>ID</th><th>错误码</th><th>类别</th><th>时间</th></tr></thead><tbody>${{rows}}</tbody></table>`
        : `<div class="empty">当前没有未处理死信。</div>`;
    }}

    function renderMemories(payload, query) {{
      const memories = payload.memories || [];
      const retrieval = payload.retrieval || {{}};
      const badge = document.getElementById("memory-result-count");
      badge.textContent = `${{memories.length}} 条结果`;
      badge.className = "badge";
      if (!memories.length) {{
        document.getElementById("memory-results").className = "empty";
        document.getElementById("memory-results").textContent = `没有找到与“${{query}}”匹配的已授权记忆。`;
        return;
      }}
      document.getElementById("memory-results").className = "memory-list";
      document.getElementById("memory-results").innerHTML = memories.map(item => `
        <article class="memory-row">
          <div>
            <div class="row-title">${{escapeHTML(item.kind || "记忆")}} <span class="badge">${{escapeHTML(item.status || "confirmed")}}</span></div>
            <div class="memory-content">${{escapeHTML(item.content || "")}}</div>
            <div class="memory-meta"><span>${{escapeHTML(item.scope || "-")}}</span><span>·</span><span>${{code(item.memory_id)}}</span></div>
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
        WORKSPACE_FORBIDDEN: ["当前 Agent 没有该工作区权限", "请核对当前管理 Agent 的工作区授权和 memory.manage 能力。"]
      }};
      return messages[code] || ["暂时无法读取管理数据", "管理页保留了本机会话和凭据边界。请稍后重新读取，或根据错误码检查 Sidecar 与 Gateway。"];
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

    async function searchMemories() {{
      const input = document.getElementById("memory-query");
      const query = input.value.trim();
      if (query.length < 2) {{
        input.focus();
        return;
      }}
      const root = document.getElementById("memory-results");
      const badge = document.getElementById("memory-result-count");
      root.className = "empty";
      root.textContent = "正在检索已授权记忆…";
      badge.textContent = "检索中";
      try {{
        renderMemories(await api("/api/memories?q=" + encodeURIComponent(query)), query);
      }} catch (error) {{
        root.className = "empty";
        root.textContent = errorCopy(error.message)[1];
        badge.textContent = "暂不可用";
        toast(error.message);
      }}
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
        const [overview, health, reviews, devices, audit, deadLetters] = await Promise.all([
          api("/api/overview"),
          api("/api/health"),
          api("/api/reviews"),
          api("/api/devices"),
          api("/api/audit"),
          api("/api/dead-letters")
        ]);
        renderOverview(overview);
        renderHealth(health);
        renderReviews(reviews);
        renderDevices(devices);
        renderAudit(audit);
        renderDeadLetters(deadLetters);
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
      document.querySelectorAll(".nav-button").forEach(item => item.removeAttribute("aria-current"));
      const button = document.querySelector(`.nav-button[data-view="${{view}}"]`);
      if (button) button.setAttribute("aria-current", "page");
      document.querySelectorAll(".view").forEach(item => item.classList.remove("active"));
      document.getElementById(view).classList.add("active");
      document.getElementById("page-title").textContent = labels[view][0];
      document.getElementById("page-subtitle").textContent = labels[view][1];
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
      }}
    }});

    document.getElementById("refresh").addEventListener("click", refreshAll);
    document.getElementById("load-error-refresh").addEventListener("click", refreshAll);
    document.getElementById("memory-search-form").addEventListener("submit", event => {{
      event.preventDefault();
      searchMemories();
    }});
    refreshAll();
  </script>
</body>
</html>""".encode("utf-8")


class _AdminConsoleHandler(BaseHTTPRequestHandler):
    state: AdminConsoleState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            params = parse_qs(parsed.query)
            launch_token = (params.get("session") or [""])[0]
            if launch_token:
                session = self.state.session.consume_launch_token(launch_token)
                if session:
                    self._redirect_with_session(session)
                    return
            if not self._authorized():
                self._json({"error": "LOCAL_ADMIN_SESSION_REQUIRED"}, status=401)
                return
            nonce = secrets.token_urlsafe(16)
            self._send_bytes(_html_page(self.state.workspace_id, nonce), content_type="text/html; charset=utf-8", nonce=nonce)
            return
        if parsed.path.startswith("/api/"):
            if not self._authorized():
                self._json({"error": "LOCAL_ADMIN_SESSION_REQUIRED"}, status=401)
                return
            self._handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self._json({"error": "NOT_FOUND"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json({"error": "NOT_FOUND"}, status=404)
            return
        if not self._authorized():
            self._json({"error": "LOCAL_ADMIN_SESSION_REQUIRED"}, status=401)
            return
        self._handle_api_post(parsed.path)

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
                if not 2 <= len(text) <= 256:
                    raise AdminConsoleError("MEMORY_QUERY_INVALID")
                self._json(sidecar.search(payload | {"query": text, "limit": 20}))
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
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}={session_token}; HttpOnly; SameSite=Strict; Path=/",
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
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise AdminConsoleError("管理控制台只能监听回环地址")
    if not 1024 <= int(port or 0) <= 65535 and int(port or 0) != 0:
        raise AdminConsoleError("PORT_INVALID")
    state = AdminConsoleState(
        workspace_id=_workspace_id(workspace_id),
        session=session or LocalAdminSession(),
        sidecar_factory=sidecar_factory,
        max_heartbeat_age_seconds=max_heartbeat_age_seconds,
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
    args = parser.parse_args()
    if not 1 <= args.max_heartbeat_age_seconds <= 3600:
        raise SystemExit("MAX_HEARTBEAT_AGE_INVALID")
    session = LocalAdminSession()
    try:
        server = create_admin_console_server(
            workspace_id=_workspace_id(args.workspace),
            host=args.host,
            port=args.port,
            session=session,
            max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
        )
    except AdminConsoleError as exc:
        raise SystemExit(str(exc)) from None
    url = f"http://{args.host}:{server.server_port}/?session={session.launch_token}"
    print(f"Memory Admin Console listening on http://{args.host}:{server.server_port}", flush=True)
    print(f"Open once: {url}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
