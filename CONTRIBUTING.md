# Contributing to Strand

Thanks for helping out. Read `ARCHITECTURE.md`, `CONSTRAINTS.md` and `.clinerules` before writing code.

## Development Environment

```bash
python3 -m venv .venv
.venv/bin/pip install -e . pytest
```

Runtime dependencies are limited to those declared in `pyproject.toml`. Do not add new ones.

## Hermetic Testing Invariant

All tests must run 100% offline at $0 token cost, with zero warnings:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -W error
```

Fix the code, never relax a test.

## Security & POSIX Invariants

- Directory sandboxing: Strand-managed folders are `0700`, enforced through `perms.secure_parent_dir`. Other parents (`$HOME`, `/tmp`) are never mutated.
- Sockets and databases (`memory.db`, `-wal`, `-shm`, `config.json`) are `0600`.
- Private egress is fail-closed: private entities are never injected unless a request explicitly opts in, and corrupt metadata counts as private.

## Code Conventions

- Type-annotate all new code.
- Keep asyncio safe: never block the event loop; use `aiosqlite` and `httpx` async APIs.
- No external database server. SQLite (WAL) plus `sqlite-vec` only, no Docker.
- Keep diffs small and focused.
