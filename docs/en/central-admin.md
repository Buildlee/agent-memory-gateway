# Central Admin UI

Run the production admin UI beside the shared-memory center: the same environment that hosts the Gateway, Worker, and metadata storage. Device status, reviews, dead letters, and activity then come from one service boundary. The local loopback console remains available as a fallback for offline maintenance.

```mermaid
flowchart LR
  B["Browser"] -->|"HTTPS /admin + one-time session"| P["Caddy reverse proxy"]
  P --> C["Central Admin Console"]
  C -->|"shared network namespace"| S["Central Admin Sidecar"]
  S -->|"short-lived token + memory.manage"| G["Memory Gateway"]
  G --> M[("Metadata, audit, and memory backends")]
```

The browser reaches only Caddy's `/admin` path. The console does not connect to the database and does not hold Gateway tokens, refresh credentials, or device private keys; it calls the existing authorized Gateway interfaces through the central admin Sidecar.

## First setup

First deploy a Gateway release containing `deploy/fn/admin-console.compose.yaml` and confirm the Gateway, Worker, and proxy are healthy. Then run this read-only preflight from Windows:

```powershell
.\scripts\setup-central-admin.ps1 `
  -SshHost "deploy-user@nas" `
  -SshPort 22 `
  -RemoteRoot "/srv/memory-gateway" `
  -StateDirectory "/srv/memory-gateway/admin" `
  -TenantId "tenant" `
  -UserId "administrator" `
  -DeviceId "memory-admin" `
  -AgentInstallationId "memory-admin" `
  -DefaultWorkspace "shared-workspace" `
  -PublicBaseUrl "https://memory-gateway.internal:8443/admin"
```

The preflight checks the Gateway, release copy, Docker network, target directory, and existing central-admin containers only. It does not create identities, write credentials, or replace containers. After reviewing the output, add `-Apply` to the same command. The first apply registers a separate central admin device and Agent, writes its device key, refresh credential, and Sidecar key to protected owner-only locations, and starts only `admin-sidecar` and `admin-console`. Most Linux filesystems show this as `0600`; some NAS mounts report the equivalent owner-only mode as `0700`.

If a central identity or admin container already exists, the script stops by default. Add `-Resume` only after confirming that those two admin containers may be replaced.

## Open the UI

Do not store passwords, Gateway tokens, or permanent browser links. Each time an administrator opens the UI, run:

```powershell
.\scripts\open-central-admin.ps1 `
  -SshHost "deploy-user@nas" `
  -SshPort 22 `
  -RemoteRoot "/srv/memory-gateway" `
  -StateDirectory "/srv/memory-gateway/admin"
```

The script recreates only `admin-console`, obtains a fresh short-lived link, and hands it directly to the default browser. The link is not echoed to PowerShell, the operations log, or Docker logs. Its first request becomes an `HttpOnly`, `Secure`, `SameSite=Strict` session cookie scoped to `/admin`.

## Network and authorization boundary

- Caddy is the only browser-facing entry. `admin-console` has no host port, and the admin Sidecar RPC listens only on container loopback.
- Access `/admin` only from the LAN or a VPN boundary. Do not publish it to the public Internet or disable TLS validation.
- The central admin identity is separate from Codex and Hermes identities. It uses the same device registration and workspace authorization model, so no machine-specific management implementation is needed.
- The UI displays only authorized device, capability, status, time, event reference, and audit metadata. It does not display raw public keys, refresh credentials, connection strings, tokens, or ciphertext.

## Acceptance

1. Gateway, Worker, proxy, and `admin-sidecar` are healthy or running.
2. Open `/admin` through the opening script and verify Overview, Reviews, Devices, Runtime, and Activity.
3. Confirm that status cards open the matching page and the device page shows device, Agent, binding, capability, and recent state without credentials.
4. Verify a deliberately confirmed review action reaches the Gateway audit trail. Do not add deletion, batch cleanup, or automatic replay to the UI.

If the central entry is unavailable, check Caddy, `admin-sidecar`, and Gateway authorization before running the opening script again. Do not bypass the path by connecting the browser to the database or by editing Hermes configuration storage.
