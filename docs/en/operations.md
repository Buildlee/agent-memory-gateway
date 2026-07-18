# Daily Operations & Recovery

Applies to administrators of deployed Gateways and local Sidecars. All checks are read-only by default — they will not replay events, drain the outbox, modify audit results, or delete data.

---

## 🔍 Health Check

Run this when the project is installed on the device and the Sidecar is running:

```powershell
.\scripts\check-admin-health.ps1 `
  -AgentInstallationId "your-admin-agent-id" `
  -DefaultWorkspace "your-workspace-id"
```

The script reads the loopback auth key from the local protected Sidecar key file, then calls the Sidecar. It will not print the key, Gateway token, refresh credentials, database connection strings, or memory body content.

When run inside a cloned source directory and the command entry point is not yet installed, the script falls back to an in-process mode using the current repository's `src` directory only — it does not install dependencies, modify the global environment, or read Gateway credentials. Behavior after a proper installation remains unchanged.

Output JSON:

- `ok: true` — worker heartbeat is normal, no retryable events or unprocessed dead letters
- `WORKER_HEARTBEAT_STALE` — the worker's most recent heartbeat has exceeded the threshold. Check Gateway and worker health status and logs
- `RETRYABLE_EVENTS_PRESENT` — events are waiting to be retried. Confirm that the network, database, and backend dependencies are available
- `DEAD_LETTERS_PRESENT` — events have stopped automatic retrying and require manual assessment

Exit code `0` means normal, `1` indicates a runtime issue, and `2` means the Sidecar, workspace configuration, or admin authorization is unavailable. Windows scheduled tasks, monitoring platforms, or notification scripts can use this to trigger alerts; do not write check output together with environment variables into public logs.

---

## 🌐 Admin UI: central by default, local as fallback

In production, run the admin UI beside the Gateway and expose it through HTTPS `/admin`; keep the `127.0.0.1` console for offline maintenance and single-device troubleshooting. Both paths require a registered identity with `memory.manage`, and neither lets the browser connect directly to the Gateway or database.

See [Central Admin UI](central-admin.md) for initialization, one-time opening, container boundaries, and acceptance checks. It uses a dedicated admin Sidecar and exposes only Caddy's `/admin` path to browsers already inside the LAN or VPN boundary.

The local fallback still starts as follows:

```powershell
.\scripts\start-admin-console.ps1 `
  -AgentInstallationId "your-admin-agent-id" `
  -DefaultWorkspace "your-workspace-id"
```

The script reads the local Sidecar key file, sets only the environment variables needed to access the Sidecar, and clears any inherited Gateway tokens, refresh credentials, device IDs, and CA configuration. After startup it prints a `http://127.0.0.1:<port>/?session=...` address — this URL is for the initial exchange of a local session cookie only and must not be reused. The central entry does not print its link; the opening script hands it directly to the browser.

The admin console, Sidecar, and Gateway must be on the same version. If the page shows `LOCAL_METHOD_UNSUPPORTED`, it means the Sidecar is still an old process — complete the version update first, restart the Sidecar on this device during a maintenance window, then re-run the health check. Do not bypass this by connecting directly to the Gateway through the browser.

The Windows Sidecar startup script preferentially loads code from the `src` directory of the current release copy. This way, when you update the source code and restart the Sidecar, you don't need to replace the `.exe` file that may be locked by Codex or the Hermes MCP. The release copy must remain at a verified version; do not set an experimental directory as the scheduled task's working directory. If the project directory has no `src`, the script continues using the installed package.

The console has six sections:

- **Overview** — pending audit, pending retries, dead letters, active devices, health checks, and recent activity; each status card opens its matching page
- **Memory** — search memories by keyword within the current workspace that the current Agent is authorized for; does not create, delete, or batch-edit
- **Audit** — confirm original text, confirm after editing, preserve both versions, resolve conflicts, reject, and archive
- **Devices & Permissions** — device and Agent names, types, status, workspace bindings, capabilities, recent state, and authorization epoch; technical identifiers stay collapsed and no public keys or credentials are exposed
- **Operations** — pending retries, unprocessed dead letters, and read-only recovery checks
- **Activity** — recent admin and audit records; does not expose body content or sensitive details

Audit actions require a second confirmation on the page before submission, and each request carries a revision and an idempotency key. The admin console does not provide buttons for deletion, batch replay, outbox draining, or direct database modification — these actions require a separate controlled process.

---

## 🔧 Read-Only MCP Troubleshooting Tools

Agents with the `memory.manage` permission can use the following read-only MCP tools:

- `memory_admin_overview` — audit, retry, dead letter, and device counts, plus worker heartbeat time
- `memory_admin_dead_letters` — unprocessed dead letter IDs, error codes, error categories, and creation times
- `memory_admin_audit` — recent operation timestamps, operators, result codes, and trace IDs
- `memory_admin_devices` — device, Agent, workspace bindings, and permission status

These tools do not return device public keys, credentials, `details_json`, event body content, or ciphertext. When troubleshooting, first correlate trace IDs and error codes with service logs in the protected environment; do not copy tokens, connection strings, or user content from logs into issues, chat records, or Git.

---

## ⏳ Recovery Sequence

**When heartbeat expires** — first verify the HTTPS endpoint and Gateway health check, then check the worker logs for database connection, migration version, or backend dependency errors. After the service recovers, re-run the read-only check to confirm the heartbeat timestamp has advanced.

**Retryable events** — normally the worker will handle them automatically according to the existing backoff strategy. Do not manually resubmit the original write requests; wait for dependencies to recover first, then observe whether the count decreases. If it keeps growing, pause new mutation operations, preserve the audit and event ledger, and identify the first error code.

**When dead letters appear** — do not directly delete records or drain the outbox. First confirm whether the event has already taken effect on the backend, then choose a remediation workflow based on audit records, delivery receipts, and audit trails. The current admin interface does not provide one-click replay or batch cleanup buttons, to avoid amplifying impact during a failure.

---

## 🧪 Recovery Drill

Conduct drills during maintenance windows or in isolated environments. After preparing a recoverable database backup and test events without sensitive information, follow this order:

1. Stop the test worker; confirm that the check command returns `WORKER_HEARTBEAT_STALE`
2. Restart the worker; confirm health check and heartbeat recover, then confirm no new duplicate events were produced
3. Simulate a recoverable backend failure; confirm events first enter the retryable state, then are processed exactly once after the dependency recovers
4. Record the drill date, error codes, recovery time, and verification results in the local operations log — do not commit the on-site address, account credentials, keys, or raw log text to the repository

Drills do not need to and should not touch real user content. If production dead letters must be handled, first back up and record the current state; any scenario involving deletion, cleanup, or batch replay must go through separate approval.
