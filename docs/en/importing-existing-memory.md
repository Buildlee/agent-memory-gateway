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

Each item should retain its source path, content hash, and batch number. Review records must show "where this came from, who confirmed it, and whether it was later superseded".

## Generate a preview

The first available step is scanning. It reads a directory and produces a JSONL preview without writing anything to the shared library:

```powershell
memory-import scan --source .\memory-folder --batch import_2026_07_03
```

Keep the preview file in a local protected directory. Check for passwords, tokens, private keys, connection strings, internal addresses, irrelevant session content, or stale status before proceeding to review.

## Review checklist

- Is the content confirmed by the user, project docs, or a trusted source?
- Is the scope narrow enough? Device info shouldn't become workspace-wide knowledge.
- Does it conflict with existing facts? Keep both sides and route to review.
- Does it contain recognizable credentials, private paths, or instruction-like content?
- Does it need an expiry date, archive status, or scheduled re-check?

Batch import's write, rollback, and crystal rebuild will continue building on the preview and review workflow, preventing old material from affecting new collaboration unchecked.

## Ongoing local memory sharing

Use `memory-import` for a one-time history migration. For a source that keeps changing, configure a local Provider, inspect records with `memory_local_preview`, then use `memory_share_selected` or the allowlisted `memory_propose_local_candidates` flow. Providers never upload local paths and never write central results back into the original system.
