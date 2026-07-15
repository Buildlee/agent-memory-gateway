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
      min-height: 32px;
      border-radius: 7px;
      padding: 0 11px;
      cursor: pointer;
      transition:
        background-color 160ms var(--ease-standard),
        border-color 160ms var(--ease-standard),
        color 160ms var(--ease-standard),
        transform 160ms var(--ease-out);
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
      grid-template-columns: 224px minmax(0, 1fr);
    }}
    aside {{
      border-right: 1px solid var(--line);
      background: var(--surface);
      padding: 18px 14px;
    }}
    main {{
      min-width: 0;
      padding: 22px 28px 40px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 650;
      margin-bottom: 22px;
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
      gap: 4px;
    }}
    .nav-button {{
      width: 100%;
      justify-content: flex-start;
      border-color: transparent;
      background: transparent;
      display: flex;
      align-items: center;
      gap: 9px;
      transition:
        background-color 180ms var(--ease-standard),
        border-color 180ms var(--ease-standard),
        color 180ms var(--ease-standard),
        transform 180ms var(--ease-out);
    }}
    .nav-button[aria-selected="true"] {{
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
    .nav-button[aria-selected="true"] .dot {{ background: var(--accent); }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0 0 5px;
      font-size: 22px;
      line-height: 1.25;
      font-weight: 700;
      text-wrap: balance;
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
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
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
      padding: 13px 14px;
      min-height: 86px;
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
      font-size: 28px;
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
      nav {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
      .nav-button {{ justify-content: center; }}
      .nav-button .dot {{ display: none; }}
      main {{ padding: 18px 16px 34px; }}
      .topbar {{ align-items: stretch; flex-direction: column; }}
      .split {{ grid-template-columns: 1fr; }}
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
        <button class="nav-button" data-view="overview" aria-selected="true"><span class="dot"></span>概览</button>
        <button class="nav-button" data-view="reviews" aria-selected="false"><span class="dot"></span>审核</button>
        <button class="nav-button" data-view="devices" aria-selected="false"><span class="dot"></span>设备</button>
        <button class="nav-button" data-view="runtime" aria-selected="false"><span class="dot"></span>运行</button>
      </nav>
    </aside>
    <main>
      <div class="topbar">
        <div>
          <h1 id="page-title">共享记忆管理</h1>
          <div class="subtle" id="page-subtitle">当前工作区：{escaped_workspace}</div>
        </div>
        <div class="toolbar">
          <button id="refresh">刷新</button>
        </div>
      </div>
      <div id="toast" class="toast" role="status" aria-live="polite"></div>
      <section id="overview" class="view active">
        <div class="grid" id="metrics"></div>
        <div class="panel">
          <div class="panel-head"><div class="panel-title">健康检查</div><span id="health-badge" class="badge">读取中</span></div>
          <div class="panel-body" id="health-panel"></div>
        </div>
      </section>
      <section id="reviews" class="view">
        <div id="review-list"></div>
      </section>
      <section id="devices" class="view">
        <div class="panel">
          <div class="panel-head"><div class="panel-title">已授权设备和 Agent</div></div>
          <div class="panel-body" id="device-list"></div>
        </div>
      </section>
      <section id="runtime" class="view">
        <div class="split">
          <div class="panel">
            <div class="panel-head"><div class="panel-title">未处理死信</div></div>
            <div class="panel-body" id="dead-letter-list"></div>
          </div>
          <div class="panel">
            <div class="panel-head"><div class="panel-title">近期审计</div></div>
            <div class="panel-body" id="audit-list"></div>
          </div>
        </div>
      </section>
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
      latestOperation: null
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
      reviews: ["审核候选", "只处理需要人工判断的候选记忆"],
      devices: ["设备与 Agent", "查看授权状态和能力，不显示凭据"],
      runtime: ["运行状态", "查看死信、审计和恢复检查结果"]
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

    function renderOverview(payload) {{
      const counts = payload.counts || {{}};
      document.getElementById("metrics").innerHTML = [
        metric("待审核", counts.pending_reviews || 0, counts.pending_reviews ? "warn" : "ok"),
        metric("待重试", counts.retryable_events || 0, counts.retryable_events ? "warn" : "ok"),
        metric("未处理死信", counts.unresolved_dead_letters || 0, counts.unresolved_dead_letters ? "danger" : "ok"),
        metric("活跃设备", counts.active_devices || 0, null)
      ].join("");
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
      const rows = (payload.entries || []).map(item => `
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

    async function refreshAll() {{
      const refreshButton = document.getElementById("refresh");
      refreshButton.disabled = true;
      refreshButton.dataset.loading = "true";
      refreshButton.textContent = "刷新中";
      document.getElementById("metrics").innerHTML = `<div class="skeleton"></div><div class="skeleton"></div>`;
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
        toast(error.message);
      }} finally {{
        refreshButton.disabled = false;
        refreshButton.dataset.loading = "false";
        refreshButton.textContent = "刷新";
      }}
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
        document.querySelectorAll(".nav-button").forEach(item => item.setAttribute("aria-selected", "false"));
        nav.setAttribute("aria-selected", "true");
        document.querySelectorAll(".view").forEach(item => item.classList.remove("active"));
        document.getElementById(nav.dataset.view).classList.add("active");
        document.getElementById("page-title").textContent = labels[nav.dataset.view][0];
        document.getElementById("page-subtitle").textContent = labels[nav.dataset.view][1];
        return;
      }}
      const actionButton = event.target.closest("[data-action]");
      if (actionButton) {{
        resolveReview(Number(actionButton.dataset.index), actionButton.dataset.action, actionButton.dataset.target);
      }}
    }});

    document.getElementById("refresh").addEventListener("click", refreshAll);
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
            self._handle_api_get(parsed.path)
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

    def _handle_api_get(self, path: str) -> None:
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
