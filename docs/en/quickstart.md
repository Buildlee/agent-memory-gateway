# Quickstart

Two paths: run the shared-memory experience locally first, or connect a deployed Gateway to a real Agent. The first path needs no account, API key, container, or database; the second preserves the security boundaries of your devices, workspaces, and credentials.

---

## Run a local demo first

Requires Python 3.10+. Run in PowerShell:

```powershell
git clone https://github.com/Buildlee/agent-memory-gateway.git
Set-Location agent-memory-gateway
.\scripts\setup-local-demo.ps1
```

The first run does three things:

1. Creates `.local-demo-venv` in the repo and installs demo dependencies.
2. Creates a temporary agent config, random tokens, and a SQLite database in `%LOCALAPPDATA%\agent-memory-gateway-demo`.
3. Starts the Gateway (listening only on `127.0.0.1`), lets two simulated agents complete one write and one cross-retrieval.

The terminal prints an object. Look at these two fields:

```text
status                : ready
cross_agent_results   : 1
```

`cross_agent_results` greater than 0 means the second agent found the demo memory written by the first. Tokens are never printed to the terminal — they exist only as local files in the demo directory. The script does not touch any running Codex, Hermes, Docker, or remote Gateway.

### Stop the demo

The Gateway keeps running as a background process. When finished, stop it with the `process_id` printed by the script:

```powershell
Stop-Process -Id <process_id>
```

Demo data is preserved and not automatically deleted. If rerunning and you see `DemoHome already exists`, specify a new directory — this prevents overwriting existing tokens and databases:

```powershell
.\scripts\setup-local-demo.ps1 `
  -DemoHome "$env:LOCALAPPDATA\agent-memory-gateway-demo-02" `
  -Port 18787
```

### What this demo verifies

| Verified | Description |
|---|---|
| Shared workspace | `demo-codex` and `demo-hermes` can only access `demo-workspace`. |
| Identity matching | The two agents use different random tokens; the Gateway identifies callers by token hash. |
| Write and retrieval | Agent1 writes a fact, Agent2 retrieves it via search. |
| Data stays local | Gateway binds only to `127.0.0.1`; no third-party models, vector APIs, or remote databases are called. |

The local demo helps you understand how things work. Device pairing, short-lived tokens, encrypted outbox, PostgreSQL metadata, and HTTPS deployment belong to the production service — the next section explains how to connect.

---

## Connect to a deployed shared service

After the admin generates one-time pairing codes, the client runs a one-time setup wizard. The `-Agent` format is `instance ID|type|display name` and can be repeated for multiple agents:

```powershell
.\scripts\setup-shared-memory.ps1 `
  -Mode device `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "local-pc" `
  -DefaultWorkspace "shared-workspace" `
  -Agent @(
    "codex-desktop|codex|Codex Desktop"
    "hermes-desktop|hermes|Hermes Desktop"
  ) `
  -InstallAutostart
```

The wizard prompts for the pairing code, then saves the refresh credential in Windows Credential Manager. The device private key, Sidecar outbox key, and local MCP config are skipped (not overwritten) if the files already exist. On first run, it also creates `.shared-memory-venv` in the repo to keep MCP dependencies out of the global Python installation.

If pairing succeeds but local setup is interrupted later, re-run the same command with `-UseExistingCredential` to continue. This requires the original device private key to still be present, and only reuses the existing Windows credential — it does not read, print, or overwrite the credential, nor does it overwrite scheduled tasks or MCP JSON.

If the Gateway uses an internal CA, add `-GatewayCaCertificate "<CA cert path>"`. Publicly trusted certificates do not need this parameter. If there is a certificate mismatch, fix the certificate chain — do not disable TLS validation.

After the command finishes, it lists the generated MCP JSON files. Import each JSON into Codex, Hermes, or another MCP client, then restart the corresponding Agent. The JSON only contains the local startup script, Agent ID, workspace, and local key file path — it does not store Gateway tokens, refresh credentials, database addresses, or private keys.

Agents running in Docker use the same identity and workspace protocol, but do not need the Windows runtime replicated into the container. Follow [Unified Access for Container Agents](container-sidecar.md) and run with `-Mode container` — it creates an MCP Bridge that listens only on the container's loopback address.

### Verify the connection

After configuration, check these steps in order inside an Agent:

1. Call `memory_sync_status` to confirm the Sidecar is online and recognizes the current Agent.
2. Call `memory_remember` to write a test message (without credentials).
3. Call `memory_search` or `memory_context` from another authorized Agent to look up that message.
4. Check the Gateway audit log to confirm the operations belong to the expected workspace.

When an MCP call omits `workspace_id`, the system uses `DefaultWorkspace`. If no default is configured, it returns `WORKSPACE_ID_REQUIRED`; if the device or agent does not belong to that workspace, it returns `WORKSPACE_FORBIDDEN`. Both errors mean you need to fill in or verify authorization information — not change the workspace name to placeholder text.

### Verify the local Sidecar separately

Check whether the scheduled task is still running at execution time:

```powershell
.\scripts\setup-shared-memory.ps1 -Mode verify
```

This only calls the Sidecar health endpoint on `127.0.0.1` — it does not read or write memories, clear the outbox, or connect to the database.

---

### Connect an Agent's existing local memory

The shared service does not take over or write back to an Agent's original store. Configure each source explicitly:

```powershell
$env:MEMORY_LOCAL_PROVIDER_CONFIG = '{"providers":[{"id":"personal-notes","type":"files","display_name":"Personal notes","paths":["<local-memory-file>"]}]}'
```

The built-in file Provider supports Markdown, JSON, and JSONL. Third-party systems can implement the same Python Provider entry point without changing the Gateway.

Call `memory_local_sources`, then `memory_local_preview`. Use `memory_share_selected` for manual selection. `memory_propose_local_candidates` only auto-proposes user preferences, project decisions, stable facts, and long-term conventions. Sensitive, instruction-like, and oversized records are blocked locally, and local paths are never uploaded.

For normal work, call `memory_context` before answering. Pass its `recall_id` to `memory_feedback` with `useful`, `pin`, `outdated`, or `incorrect`. Feedback adjusts future ranking within a fixed bound; it never directly deletes or rewrites a memory.

---

## Common issues

| Message | Check first |
|---|---|
| `DemoHome already exists` | The script refuses to overwrite old data. Specify a new `-DemoHome`, or confirm whether the old demo data is still needed. |
| Port already in use | Specify another `-Port`, e.g. `18787`. |
| Dependency installation failed | Python version, network, organization package source, or pip configuration. The virtualenv is preserved — fix the issue and re-run the script directly. |
| `WORKSPACE_ID_REQUIRED` | Both the Sidecar and MCP startup parameters should provide the same registered workspace. |
| `WORKSPACE_FORBIDDEN` | The admin has not yet granted the device or agent access to this workspace. |
| `GATEWAY_UNAVAILABLE` | The local Sidecar is not running, the Gateway address is unreachable, or the TLS certificate chain is not configured correctly. |
| MCP config already exists | The setup wizard refuses to overwrite. Confirm whether the existing config is still in use, then choose a new `-McpOutputDirectory`. |
| Runtime environment incomplete | `.shared-memory-venv` exists but is missing dependencies. The script will not delete it automatically — investigate the cause and handle it manually. |
| Setup interrupted after pairing | Keep the original device private key, re-run with the same parameters plus `-UseExistingCredential`, and do not reuse an expired pairing code. |

---

## Next steps

- Need server deployment, migration, or go-live checklist → [Deployment Guide](deployment.md)
- Need to understand permissions, auditing, offline sync, and retrieval policies → [Overall Design](design-v2.md)
- Need complete Codex, Hermes, or OpenClaw examples → [Integration Examples](../../examples/README.md)
