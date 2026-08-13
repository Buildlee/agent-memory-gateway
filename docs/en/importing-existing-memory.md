# Importing Existing Memory

`MEMORY.md`, `USER.md`, project notes, and local records can speed up shared library initialization. Since sources have different credibility, treat them as pending review material first.

## Prepare materials

Separate sources before feeding them to the scanner. Content worth prioritizing includes confirmed preferences, project decisions, device facts, and long-lived conventions.

| Source | Typical scope | Notes |
|--------|--------------|-------|
| `USER.md` | `user` | Keep only confirmed, durable preferences |
| `MEMORY.md` | `workspace` or `agent` | Distinguish project consensus from agent-specific experience |
| `SOUL.md` | `agent` or `private` | Don't auto-promote role settings to workspace knowledge |
| Local paths, ports, hardware | `device` | Only expose to agents that need them |
| Architecture decisions | `workspace` | Attach source and confirmation timestamp |
| Old task states | `session` or `archived` | Keep as history, not current facts |

## Process

```
Local material
  → Scan and generate preview
  → Sensitive content check
  → Classify, chunk, assign scope
  → Dedup and conflict check
  → Human review
  → Write to shared library
```

The local preview retains a relative source path, content hash, and batch number. The central event receives only a stable source-record ID, content hash, and batch number; local paths are never uploaded. A local state file keeps event IDs and backend references for resume and rollback.

## Generate a preview

The first available step is scanning. It reads a directory and produces a JSONL preview without writing anything to the shared library:

```powershell
memory-import scan --source .\memory-folder --batch import_2026_07_03
```

Keep the preview file in a local protected directory. Check for passwords, tokens, private keys, connection strings, internal addresses, irrelevant session content, or stale status before proceeding to review.

Sensitive chunks are marked `blocked_sensitive`, instruction-like chunks `blocked_instruction_like`, and oversized chunks `blocked_too_large`; only `imported_candidate` records can be applied.

## Apply and resume

After reviewing the JSONL, submit accepted candidates through the running loopback Sidecar:

```powershell
memory-import apply `
  --preview .\import-preview-import_2026_07_03.jsonl `
  --workspace-id shared-workspace `
  --agent-installation-id codex-desktop `
  --confirmed-by-user
```

`apply` processes only `imported_candidate` records and uses the normal authorization, sensitive-content, outbox, and sync paths. It never connects directly to the database. State is written beside the preview as `.state.json`; after interruption, rerun with the same preview and workspace plus `--resume`. Stable event IDs prevent duplicate imports. `sync_pending` means connectivity must recover before retrying; `review_pending` means records entered the normal review queue; `completed` is returned only after durable backend references are available without errors.

Pass `--sidecar-key-file` or `--sidecar-port` when the local installation does not use defaults. The importing Agent needs the target workspace's write and sync capabilities.

## Roll back a batch

Rollback archives imported memories through the normal `memory.forget` path; it does not hard-delete them:

```powershell
memory-import rollback `
  --state .\import-preview-import_2026_07_03.jsonl.state.json `
  --agent-installation-id codex-desktop `
  --confirmed-by-user
```

Already archived entries are skipped. Events without a backend reference appear in `pending_event_ids`, while archive operations that did not complete appear in `failed`. Restore sync or resolve the reported error, then run rollback again. Rollback accepts backend references only from the Sidecar's durable local receipts and does not trust editable reference fields in the state file. Keep the state file until acceptance or rollback is complete.

## Review checklist

- Is the content confirmed by the user, project docs, or a trusted source?
- Is the scope narrow enough? Device info shouldn't become workspace-wide knowledge.
- Does it conflict with existing facts? Keep both sides and route to review.
- Does it contain recognizable credentials, private paths, or instruction-like content?
- Does it need an expiry date, archive status, or scheduled re-check?

Imported evidence is marked as explicitly confirmed by the user. Conflicts can still enter the normal review and supersession workflow; crystal rebuild remains an explicit administrative action.

## Ongoing local memory sharing

Use `memory-import` for a one-time history migration. For a source that keeps changing, configure a local Provider, inspect records with `memory_local_preview`, then use `memory_share_selected` or the allowlisted `memory_propose_local_candidates` flow. Providers never upload local paths and never write central results back into the original system.
