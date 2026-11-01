# Strand

**Digital continuity for Linux: a sovereign, local working memory for your LLM tools.**

Strand is a headless system daemon that sits between your local frontends (editors, CLI pipes, launchers, the bundled GUI) and a frontier LLM. It exposes a standard `/v1/chat/completions` proxy, resolves the relevant subgraph of what it already knows about you and your work, injects a deduplicated context delta into the prompt, and forwards the call. Your memory stays on your machine, in a single local database you own.

## Highlights

- **Digital Continuity.** Projects, hardware, collaborators and standing preferences persist across sessions, tools and models. Recent uncompacted episodes are stitched back into new sessions.
- **Kernel-adjacent IPC.** The daemon listens on a UNIX domain socket, `$XDG_RUNTIME_DIR/strand/strand.sock`, so access is governed by filesystem permissions (directory `0700`, socket `0600`) instead of an open network port. Loopback TCP at `127.0.0.1:8088` is the fallback.
- **Embedded storage.** SQLite in WAL mode plus [`sqlite-vec`](https://github.com/asg017/sqlite-vec) for vector search. Memory is stored as entity-relationship triples (`entities`, `edges`, `episodes`), not loose markdown files. There is no external database server and no Docker.
- **Fail-closed privacy.** Entities flagged private are never injected unless the request opts in with `X-Strand-Allow-Private`. Corrupt metadata is treated as private.
- **Dynamic Personality Matrix.** Three archetypes shape how the assistant behaves:
  - `trait`: character and demeanor.
  - `directive`: inflexible behavioral rules.
  - `preference`: stylistic output formats.

  They are linked from the `User` entity (`enforces`, `demands`, `prefers`) and injected once per session. **Calibration** is continuous: when you contradict a rule, the old edge is deprecated (never deleted) and the new one takes over.
- **Provenance and confidence.** Every fact records its origin (`user_explicit`, `model_inferred`, `system`) and a confidence. Explicit facts are never displaced by guesses, and low-confidence recall is tagged `[inferred]`.

## Getting started

Requires Python 3.11+ and Linux with systemd (user session).

```bash
./scripts/install.sh               # one step: pipx install, desktop entry, icon, systemd user service, model warm-up, `strand doctor`
./scripts/install.sh --editable    # same, but an editable pipx install for development
strand chat                        # interactive terminal chat
strand-gui                         # native desktop chat window
```

The installer needs `pipx` (`dnf install pipx`, `apt install pipx` or `pacman -S python-pipx`), Python 3.11+ and a `systemd --user` session. It refuses to run as root and is safe to re-run.

No systemd? Run it in the foreground with `strand daemon --diag`.

### `strand chat`

A streaming REPL that talks to the daemon over `strand.sock` (TCP fallback).

| Input | Effect |
| --- | --- |
| `/help` | list commands |
| `/new` | start a fresh session |
| `/private on\|off` | send or stop sending `X-Strand-Allow-Private` |
| `/stats` | show entity, edge and episode counts |
| `exit`, `quit` | leave |

`Ctrl+C` aborts the stream in progress; pressing it twice exits. Options: `--model NAME`, `--private`.

### `strand-gui`

A PySide6/QML window using the same socket. Shortcuts: `Ctrl+L` new session, `Ctrl+D` diagnostics, `Esc` abort. Closing the window ends the process and its I/O worker threads.

### Operator CLI

`strand stats | list | inspect | drop | prune-stale | prune-decayed | embed-backfill | compact | config | logs`. These work directly on the database, so the daemon does not need to be running.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): schema, modules, security invariants.
- [ROADMAP.md](ROADMAP.md): shipped phases and what is next.

## Development

```bash
pip install -e . pytest
pytest -W error
```
