# Strand Constraints (Permanent Invariants & Non-Goals)

These are the negative boundaries and core laws of Strand. Every collaborator, human or AI, must read this file (with `ARCHITECTURE.md` and `.clinerules`) before writing code. A change that violates anything here is rejected, not negotiated.

## 1. Identity & Core Architectural Lane
- Strand is a sovereign, local, headless cognitive working-memory daemon. It runs as a `systemd` user service over a private UNIX domain socket (socket `0600`, parent directory `0700`).
- Strand is **NOT** an operating system, a desktop window manager or a visual orchestrator. That lane belongs to Aether.
- Strand is **NOT** a full-text filesystem scraper, a code AST repository, or a background daemon that indexes disks.

## 2. The Memory Graph Law (`memory.db`)
- `memory.db` stores **only** typed entities, directional edges, episodes, and high-level behavioral stances (`trait`, `directive`, `preference`).
- **Raw blobs never enter `memory.db`.** Code files, diff buffers, AST trees, terminal dumps and base64 image strings must never be written to any SQLite table. Physical truth stays on disk and in Git. Cognitive truth stays in SQLite.
- **Independent substrate.** Do not attach, sync or integrate secondary indexer databases (e.g. a Weaver DB). Live grounding happens just-in-time (JIT).

## 3. Filter for New Features and External Hooks (The 3 Laws)
Every proposal must pass **all three** criteria:

1. **Ephemeral Runtime vs. Durable Memory.** Transient data stays in memory for the active turn only. Only distilled relational takeaways persist to SQLite.
2. **Just-in-Time (JIT) Pull vs. Background Sync.** Never crawl, sync or duplicate local state in the background. Pull live, on demand, through fast scoped tools.
3. **Transport & Privacy Boundaries.** Preserve kernel-adjacent security: POSIX `0700`/`0600` file modes, the private local UDS socket, fail-closed egress and zero telemetry.

## 4. Capability Ring Model for Host Awareness
| Ring | Scope | Rule |
|------|-------|------|
| **Ring 0: Ambient Read** | Autonomous, read-only | Restricted strictly to approved `workspace_roots` (`~/Projects/**`). Windowed pagination and hard size caps (e.g. max 8 KB per call). |
| **Ring 1: Git-Guarded Mutation** | Writes | Allowed only inside git-tracked repositories, with checkpoint/undo capability. |
| **Ring 2: Gated Shell & Egress** | Arbitrary shell, package managers, root | Requires explicit operator confirmation every time. |

Anything not covered by a ring is denied by default.
