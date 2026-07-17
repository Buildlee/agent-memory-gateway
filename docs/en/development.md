# Development & Validation

This document records the development conventions for the repository. Use only generic paths and variable names; local machine accounts, server addresses, certificates, keys, and field operation records must not be included.

## Local Development Baseline

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,postgres,dev]"
python -m unittest discover -s tests
python -m compileall -q src tests
```

Confirm both tests and compilation pass before modifying functionality. After modifications, run at least the affected module's tests, then run the full test suite.

---

## CI Validation

GitHub Actions runs on feature branches and PRs: full tests, Python compilation, PowerShell syntax checks, sensitive information scanning, and patch format checks. It does not read environment files, certificates, databases, Windows credentials, or field operation scripts.

The security classifier's dedicated corpus includes deliberately invalid private keys, tokens, and connection strings to prove that the detection rules work. CI literal scanning only excludes this fixed test corpus; the corresponding unit tests confirm that these samples carry an explicit invalid marker.

A passing CI indicates that the public code is mergeable, not that the functionality has been deployed to a running Sidecar. When new local capabilities (such as a management page) are needed, the device must still be updated during a maintenance window and read-only health checks must be run.

---

## Local Experience Script

`scripts/setup-local-demo.ps1` is the entry point for the first-time experience. It creates a repo-ignored `.local-demo-venv`, installs the project, and calls `scripts/start-local-demo.ps1` to launch a SQLite Gateway listening only on `127.0.0.1`. The demo principal, tokens, database, and logs are stored in `DemoHome` outside the repository.

The script defaults to having two simulated Agents complete one cross-retrieval. When modifying the demo script, use `[System.Management.Automation.Language.Parser]::ParseFile` to check for syntax errors, and verify that:

- `DemoHome` is located outside the repository and refuses to overwrite an existing directory.
- The Gateway binds only to `127.0.0.1` and refuses to start if the port is already in use.
- Random tokens are not written to the terminal, logs, repository, or example files.
- The second Agent can retrieve the test records written by the first Agent.

See [Quickstart](quickstart.md) for full usage instructions.

---

## Sidecar Deployment

`start-sidecar.ps1`, `install-sidecar-autostart.ps1`, and `start-sidecar-mcp.ps1` all require a `DefaultWorkspace` parameter, which must be a registered workspace ID. This value is used when an MCP call does not include a `workspace_id`; an error is raised if it is not configured.

When the current release copy contains a `src` directory, the startup scripts load it via `PYTHONPATH` without writing to the global Python environment. This avoids Windows locking the `.exe` launcher file during long-running MCP client sessions. When modifying startup paths, run at least the full test suite, PowerShell syntax checks, and `tests.test_release_safety`.

The `SshPort` for remote deployment scripts must be between 1 and 65535, defaulting to 22. Both SSH commands and SCP uploads use the same port.

If the local repository is still on a development branch before a release, use `-ProjectRoot` to point to a verified release copy on the main branch. The script verifies the release copy's required files before starting remote operations.

Migration SQL is maintained in the root `schema/` directory and is also shipped as a read-only copy within the package at `agent_memory_gateway/_schema`. Containers and source-directory installations prefer the root copy; installed packages automatically use the in-package copy. After modifying SQL files, both locations must be kept in sync and the full test suite must be run to prevent local installations, Windows Sidecars, and containers from computing different checksums.

---

## Setup Wizard Regression Points

`scripts/setup-shared-memory.ps1` is the actual integration entry point, not an alias for the demo script. When modifying it or the device pairing client, run at least:

```powershell
python -m unittest tests.test_device_pair tests.test_setup_installer tests.test_release_safety
python -m compileall -q src tests
```

The wizard must maintain the following behaviors:

- Pairing codes are only read from stdin via `Read-Host -AsSecureString`.
- Refreshed credentials are only written to the Windows Credential Manager (`write_generic_credential`).
- The MCP JSON contains only the command and arguments, not the Gateway token, refresh credentials, or private keys.
- Existing local keys, scheduled tasks, runtime environments, and MCP JSON are all refused for overwrite.
- Recovery after pairing only allows `-UseExistingCredential` and requires the original device private key to exist.
- Server mode does not connect to the remote endpoint or create a release directory without `-Apply`.
- Publicly trusted HTTPS addresses should not fail due to a CA that does not exist by default; internal CAs must be explicitly provided by the user and validated.

---

## Hybrid Retrieval

The retrieval code lives in `src/agent_memory_gateway/hybrid_retrieval.py`. It receives candidates that have already passed authorization filtering and does not itself determine who can read memories.

- Lexical matching handles English, numbers, and common symbols.
- Chinese text is split into single characters and adjacent bigrams, enabling retrieval of records containing Chinese phrases.
- Identical features produce fixed local hash vectors to supplement pure text matching. No network requests, no third-party vector API.
- Candidates with identical normalized content, or a vector similarity ≥ 0.94, are deduplicated by keeping only the highest-ranked one.
- MMR re-ranking: `0.80 * base_score - 0.20 * similarity + group_bonus`, avoiding overly similar results to already selected items and preferentially filling in different scopes or types.

Scoring formula: `base_score = 0.50 * lexical + 0.35 * vector_score + 0.15 * confidence` (when a query is present); without a query, confidence is used directly.

`PostgresQueryService` first retrieves the `backend_ref` visible to the current principal, then requests the corresponding facts from GBrain. If unauthorized facts are mixed into the results, they are discarded server-side (`source is None → continue`) and will not appear in the response.

---

## Context Budget

The `max_tokens` of `memory_context` must be between 64 and 12,000, defaulting to 1,200 (constant defined in `hybrid_retrieval.py`; `sidecar_mcp.py` default value is 1200). This limits the estimated token count of memory references, excluding safety instructions and JSON field overhead.

Each reference is first estimated for its content, then a fixed overhead is added; the `token_estimate` of selected references must not exceed `token_budget`. When a candidate does not fit, the entire item is skipped without truncating the body text. The response includes:

- `retrieval.candidate_count`: the number of candidates entering hybrid ranking
- `retrieval.duplicate_count`: the number excluded due to duplication
- `retrieval.budget_skipped_count`: the number not returned due to budget constraints
- `incomplete`: set to `true` when budget limits prevented some candidates from being returned

An invalid budget value (out of range or non-numeric) returns a stable error `MAX_TOKENS_INVALID` / `MAX_TOKENS_OUT_OF_RANGE` without silently scaling.

---

## Offline Mode

The Sidecar only retrieves from authorized local caches and the encrypted outbox. It uses the same retrieval and budget logic, writing `offline: true, incomplete: true` to the results (via `_offline_search` and `_offline_context` in `sidecar_client.py`). When the Gateway returns authentication or permission errors, the Sidecar does not serve from cache; the cache is only used when the network is unavailable or the service is unresponsive.

---

## Test Regression

```powershell
python -m unittest tests.test_hybrid_retrieval
python -m unittest tests.test_query_service tests.test_sidecar_sync
python -m unittest discover -s tests
python -m compileall -q src tests
```

Covers Chinese matching, text normalization deduplication, sort stability, diversity, budget caps, unauthorized fact filtering, and offline Sidecar behavior. Before committing, run `git diff --check` to scan for real domain names, internal network addresses, accounts, tokens, private keys, or local file paths introduced by the change.

---

## Administration Interface

Admin data first goes through Gateway authorization, then is retrieved via the Sidecar. The browser or MCP cannot bypass this path to read PostgreSQL directly.

```powershell
python -m unittest tests.test_admin_service tests.test_gateway_admin
python -m unittest tests.test_sidecar_daemon tests.test_sidecar_mcp
python -m unittest tests.test_admin_check tests.test_admin_console
python -m compileall -q src tests
```

- The admin interface requires `memory.manage` (in `gateway.py`, `/v1/admin/*`, `/v1/reviews/*`, `/v1/crystals/rebuild` all map to this capability); without this permission, `CAPABILITY_FORBIDDEN` is returned.
- Overview returns only counts and worker heartbeats; the device list does not return public keys or credentials; the audit list does not return `details_json` or memory body text; the dead-letter list returns only stable IDs, error codes, categories, and timestamps.
- Every query is filtered by the caller's tenant, user, and workspace. When a workspace is missing or unauthorized, no default workspace is returned.
- Sidecar RPC only allows declared admin methods, using the existing short-term token and local loopback authentication.
- `memory-admin-console` only listens on the loopback address (`host="127.0.0.1"`, validated at startup). The initial URL contains a one-time `session` token that is invalidated after being exchanged for an HttpOnly Cookie (`consume_launch_token` marks it as used). Page source code, API responses, and test assertions do not contain local keys, Gateway tokens, or refresh credentials.
- Requests that change review status must include `confirmed_by_user: true`, `expected_revision`, and `idempotency_key`.

`memory-admin-check` is intended for scheduled tasks or external monitoring. It retrieves an overview from the Sidecar, detects worker heartbeats, pending retry events, and unprocessed dead letters, and outputs JSON without body text or credentials:

- Exit code `0`: status is normal
- Exit code `1`: issues found (expired heartbeat, retry events, dead letters)
- Exit code `2`: local Sidecar, configuration, or authorization unavailable

On Windows, this is launched via `scripts/check-admin-health.ps1`; the script only reads the Sidecar key from a protected local file and does not write it to output.
