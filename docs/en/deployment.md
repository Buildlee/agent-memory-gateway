# Deployment Guide

> This document describes the general workflow only and does not contain site-specific information. The domain names, paths, device IDs, database connection strings and secrets shown in examples **must** be replaced with your local configuration; do not commit real values to Git.

---

## Choose Your Path First

- **Just want to verify shared memory read/write** — run the local demo from the [Quickstart](quickstart.md). It starts a local SQLite Gateway against your existing services, no changes needed.
- **Already have a Gateway, just need one machine's Agent to connect** — jump straight to [Running the Sidecar on Each Client Device](#running-the-sidecar-on-each-client-device).
- **Preparing a new production service** — proceed step-by-step from environment checks through migration to container startup.

---

## Pre-flight Checklist

- Python ≥ 3.10.
- Production environment requires PostgreSQL, a container runtime, and an HTTPS reverse proxy.
- Gateway, Worker, database, and Agent clients should each run under a least-privilege account or restricted network.
- Prepare independent secrets: event encryption, token signing, refresh replay protection, sensitive-information fingerprints, and the Sidecar outbox **must not** reuse the same value.
- Back up existing databases and runtime configurations **before** running any migration.

The repository retains only variable templates such as `deploy/fn/.env.example`. Environment files, main configuration, certificates, and private keys that you create from these templates should be placed in a protected local directory already excluded by `.gitignore`.

When the admin UI should run beside the Gateway, configure its separate admin Sidecar and one-time browser session through the [Central Admin UI guide](central-admin.md). Do not expose the local loopback console to the LAN.

---

## Self-Check on the Deployment Machine

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,postgres,dev]"
python -m unittest discover -s tests
python -m compileall -q src
```

Tests passing only confirms the code baseline is usable — it does **not** mean your production configuration is correct.

---

## Deploy with the Setup Wizard

Once you have prepared your secrets, certificates, database backups, and protected environment files, you can use a single entry point for deployment. On the first run, omit `-Apply` to only review the parameters and see which SSH host, port, and Gateway name will be used:

```powershell
.\scripts\setup-shared-memory.ps1 `
  -Mode server `
  -SshHost "deploy-user@server" `
  -SshPort 22 `
  -RemoteRoot "/srv/memory-gateway" `
  -SecretsFile "/srv/memory-gateway/secrets.env" `
  -GatewayAddress "memory-gateway.internal"
```

After entering the maintenance window and confirming backups and migration status, append `-Apply` to the same command. Only then will it create the deployment directory, upload public code, build the image, and start Gateway, Worker, and the HTTPS proxy.

The script will **not** replace protected environment files, generate or print secrets, or execute database migrations.

---

## Migrate First, Then Start

The production Gateway uses a separate metadata database. First run a read-only check:

```powershell
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --check
```

After confirming version, extensions, permissions, and backups are correct, proceed with:

```powershell
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --apply
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --verify
```

If you are using the optional PostgreSQL long-term memory backend, also check and migrate it:

```powershell
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --check
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --apply
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --verify
```

Migration commands must **not** be chained into the Gateway startup script. The runtime account should not have database creation, role creation, or schema management permissions.

---

## Launch Gateway, Worker, and HTTPS Entry Point with Containers

The repository provides three Compose files for different services:

| File | Purpose |
|---|---|
| `deploy/fn/compose.yaml` | **Core services**: Gateway + Worker + Caddy HTTPS proxy |
| `deploy/fn/admin-console.compose.yaml` | **Admin console**: standalone admin Sidecar + web management UI (layered on top of core) |
| `deploy/fn/memory-mcp-bridge.compose.yaml` | **Container Bridge**: connects Docker-based Agents to shared memory via shared network namespace (independently deployed) |

### Launch Core Services

```powershell
docker compose --env-file "<protected environment file path>" -f deploy/fn/compose.yaml config
docker compose --env-file "<protected environment file path>" -f deploy/fn/compose.yaml up -d --build
```

Always run `config` first to verify that secrets, database ports, and volume mappings are not exposed to the public network. Gateway and Worker use the same image version. Only the proxy should expose an HTTPS entry point; the database should not be mapped to a public host port.

### Layer on Admin Console

The admin console depends on core services already running. After following the [central admin setup](central-admin.md), launch with dual Compose files:

```powershell
docker compose --env-file ".env" -f deploy/fn/compose.yaml -f deploy/fn/admin-console.compose.yaml up -d admin-sidecar admin-console
```

### Container-Based Agent Access

Docker-based Agents (e.g., NAS Hermes) use the generic Bridge template, sharing the network namespace with the target container. See [Container-Based Agent Integration](container-sidecar.md).

---

## Running the Sidecar on Each Client Device

Start **exactly one** Sidecar per device. We recommend using the setup wizard for one-time pairing, isolated runtime, scheduled task, and MCP configuration generation:

```powershell
.\scripts\setup-shared-memory.ps1 `
  -Mode device `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "local-pc" `
  -DefaultWorkspace "shared-workspace" `
  -Agent "codex-desktop|codex|Codex Desktop" `
  -InstallAutostart
```

The administrator provides a one-time pairing code in advance. The wizard reads it via hidden input, and after successful pairing stores the refresh credential in Windows Credential Manager. The generated MCP JSON files are placed in `%LOCALAPPDATA%\memory-gateway\mcp`; import them into the corresponding client and restart the Agent. If you need custom directories, manual Sidecar startup, or non-Windows environments, you can still use `start-sidecar.ps1`, `install-sidecar-autostart.ps1`, and the [example documentation](../../examples/README.md).

The Sidecar listens only on the local loopback address. Connect to the internal HTTPS address directly within the LAN; from outside the LAN, reach it through a VPN, zero-trust network, or controlled tunnel back to the same network boundary. Regardless of the network path, **never disable TLS certificate verification**.

When upgrading the Gateway, ensure the Gateway, Worker, and every Sidecar use compatible versions. Complete the server-side upgrade and health check first, then restart Sidecars one by one during a maintenance window. After each restart, immediately perform a read-only health check. Do not bypass old Sidecars using a browser, script, or direct database connection.

When a Windows scheduled task launches the Sidecar from a release copy, it should prefer that copy's `src` directory. First place the validated release copy in the task's working directory, then restart the Sidecar during a maintenance window; do not forcibly replace `.exe` startup files while the MCP client is running. If the release copy has no source directory, the startup script falls back to the installed package.

### deploy-fn-release.ps1 Notes

`scripts/deploy-fn-release.ps1` defaults to SSH port 22. If your FnOS uses a different port, explicitly pass `-SshPort <port>` (range 1–65535). The script uses the same port for remote checks, source upload, build, and startup — this ensures it does not reach a different host after the validation step.

By default, the script deploys from the repository it resides in. When you need to build from an independent release copy, pass `-ProjectRoot <validated directory>`; this directory must contain `pyproject.toml`, `README.md`, `src`, `schema`, and `deploy`. This allows you to explicitly deploy only the merged version when there are uncommitted local changes.

### DefaultWorkspace

`DefaultWorkspace` is **not** a placeholder name — it is a formally registered workspace ID. The MCP configuration must pass the same value at startup. If a tool does not specify a workspace, it will error out immediately rather than guessing or switching to a different workspace.

Configuration files and field descriptions for Codex, Hermes, and OpenClaw are available in [examples/README.md](../../examples/README.md). The MCP configuration should contain only the script path and Agent installation instance ID; secrets are managed by the local protected storage and the Sidecar.

### Agent Inside a Container

Agents running inside Docker do not need a dedicated version. Use the [Container Sidecar](container-sidecar.md) with `-Mode container`. The installer identifies the target container project from its Compose labels and starts a generic `memory-mcp-bridge`. The bridge shares the network namespace with the target container and only exposes `http://127.0.0.1:8767/mcp` — it does **not** bind to a host port. The application's own MCP settings should still be configured through its official UI or API; do not modify the application's database.

---

## Pre-launch Checklist

Check in order:

1. Gateway and Worker health checks pass.
2. Database migration `verify` passes and the runtime account has least-privilege permissions.
3. Registered devices can obtain short-lived tokens; unregistered devices are rejected.
4. One Agent can write, search, and retrieve context; another authorized Agent can see results in the same scope.
5. When the client network is disconnected, events enter the encrypted outbox; after reconnection, they are synchronized exactly once.
6. Submit a merge-conflict candidate and complete one review, revert, or archive — confirm the audit trail exists.
7. Review logs, MCP configuration, Compose configuration, and the Git staging area to confirm that no real secrets, tokens, connection strings, certificates, or local paths are present.

---

## Upgrade or Recovery Sequence

- Before upgrading, record current image and migration versions, and back up the database and protected configuration.
- Run `check` on a replica or during a maintenance window **before** applying new migrations and running `verify`.
- On failure, do not replay write scripts repeatedly. First inspect the event ledger, delivery receipts, dead-letter queue, and backend references, then let the Worker reconcile.
- Credential rotation order: generate new value → update consumers → verify → revoke old value. Never overwrite a secret that is still in use.

On-site operational records are stored in ignored files and should not be committed with the code.
