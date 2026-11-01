# Strand UX & Ergonomics Observations Log

This document tracks real-world friction points, visual bugs, design observations, and security/packaging findings captured during dogfooding sessions on ONYX.

---

## 1. Active Issues & Pre-Publishing Blockers (Triaged)

### `SEC-PERM-001`: Indiscriminate Parent Directory `chmod 0700` — **RESOLVED**
* **Component:** `src/strand/db.py`, `daemon.py`, `config.py` (`DatabaseManager.connect`, `prepare_socket`, `save_config_value`)
* **Symptom:** Setting `STRAND_DB_PATH=~/memory.db` or pointing a socket directly at `~/strand.sock` causes Strand to execute `chmod 0700 ~`, altering permissions on the operator's entire `$HOME` directory.
* **Root Cause:** POSIX permission enforcement calls `os.chmod(path.parent, 0o700)` blindly without verifying whether Strand created the directory or if it is a pre-existing root directory.
* **Resolution (implemented):** new `src/strand/perms.py` (`is_managed_dir`, `secure_parent_dir`) is used by `DatabaseManager.connect`, `prepare_socket`, `save_config_value` and the GUI bridge config writer. A directory is Strand-managed only when its own name is `strand` (and it is not `$HOME`); only those get `chmod 0700`. Any other parent (`$HOME`, `/tmp`, arbitrary dirs) is never chmod-ed: a warning is logged if it is group/world accessible, while the DB/WAL/SHM, `config.json` and socket stay `0600`. Missing directories are still created with `mode=0o700`. Regression tests: `tests/test_perms.py`.
* **Original Resolution Plan:**
  * Enforce `0700` strictly on directories created and managed directly by Strand (e.g., `~/.config/strand`, `~/.local/share/strand`, `$XDG_RUNTIME_DIR/strand`).
  * If the parent path is `$HOME` or an unmanaged directory, enforce permissions on the leaf file/socket (`0600`) and emit a warning rather than modifying parent directory permissions.

### `ERG-PATH-004`: Relative Path Sandbox Resolution Under `systemd --user` (Resolved)
* **Status:** Resolved.
* **Component:** `src/strand/tools/workspace.py` (`is_safe_path`, `read_file`)
* **Symptom:** Under `systemd --user`, the working directory defaults to `/home/<user>`. When an LLM executes a tool call using a relative path (e.g., `src/strand/proxy.py`), `Path.resolve()` evaluates it against `$HOME` rather than project directories, returning a false-positive sandbox error (`403: Path outside approved workspace`).
* **Root Cause:** Relative path checking evaluates against `os.getcwd()` rather than iterating through configured `workspace_roots`.
* **Resolution Plan:**
  * In `is_safe_path()`, resolve relative paths against each declared entry in `config.workspace_roots` in addition to the current working directory.
  * Allow execution if the candidate path cleanly resolves inside any approved root.
* **Architectural Resolution:** `is_safe_path()` expands `~` first. Absolute paths are resolved and must be inside a root. Relative paths are resolved against each canonical root in order (an existing file wins, otherwise the first bounded candidate), then against `os.getcwd()`, accepted only if the result lands inside an approved root. `../` escapes from every candidate stay fail-closed (`access_denied`). `read_file` and `list_directory` share this resolver. No schema change. Regression tests live in `tests/test_tools.py`.

### `OBS-ERG-003`: Stream Throughput Pacing & Grounding Transparency
* **Component:** `src/strand/ui/bridge.py` & `main.qml`
* **Symptom:** Flash-Lite delivers responses at 300–400 t/s, snapping multi-paragraph responses onto the screen in under 800 ms. The instant render feels visually jarring and obscures whether local memory retrieval took place.
* **Exploration Paths:**
  * Pacing: Implement thought-by-thought or natural block release (~60–80 t/s) in `bridge.py` rather than a brittle character-by-character typewriter simulation.
  * Transparency: Briefly pulse the header indicator or traversed node pill for ~150 ms prior to stream release to signify local graph traversal.

### `OBS-POL-005`: Epistemic Evolution & Dynamic Persona Anchor
* **Component:** `src/strand/proxy.py`, `distill.py`, `compactor.py`
* **Symptom:** The static `persona_prompt` in `config.json` is rigid and risks drifting from the operator's actual tone and cadence over weeks of use.
* **Refined Policy:**
  * Transient Bootstrap: Treat the factory `persona_prompt` as an ephemeral umbilical for Day Zero before deferring to dynamically distilled nodes.
  * Continuous Evolution: Allow conversational critiques to deprecate old edges (`weight: 0.0`, `metadata.deprecated: true`) in favor of new directives, letting inactive traits decay naturally over time.

---

## 2. Resolved Observations & Regressions

### `SEC-PRIV-002`: Day-Zero Host Hardware Fingerprinting — **Resolved**
* **Component:** `src/strand/proxy.py`, `host.py`, `config.py`, `session.py`, `ui/bridge.py`
* **Symptom (was):** On Day Zero Strand injected the workstation's kernel/OS release, CPU model, RAM and hostname into the payload sent to the upstream model, with no opt-out.
* **Resolution:** `Config.enable_host_telemetry` (default `False`; `STRAND_ENABLE_HOST_TELEMETRY`, `config.json`, `strand config get/set`, GUI `SETTINGS_KEYS`). Telemetry is allowed only if it is `True` or the session override (`SessionLedger.set_allow_host_telemetry`, driven by the `X-Strand-Host-Telemetry` header) is active. When disallowed, `host.redact_host_context` strips OS, desktop, CPU and RAM; the hostname is kept so `<Operator> --[operates_host]--> <Hostname>` anchoring still works. No schema changes. Tests: `tests/test_proxy.py`, `tests/test_config_logging.py`, `tests/test_onboarding.py`.

### `SEC-CACHE-003`: Shared `/tmp/fastembed_cache` Isolation — **Resolved**
* **Component:** `src/strand/embed.py` (`EmbeddingManager`, `fastembed_cache_dir`, `ensure_cache_dir`), `cli.py` (`doctor`), `packaging/systemd/strand.service`
* **Resolution:** `EmbeddingManager` now builds `TextEmbedding(model, cache_dir=...)` with an isolated cache: `$XDG_CACHE_HOME/strand/fastembed` when set (absolute), else `~/.cache/strand/fastembed`; `$FASTEMBED_CACHE_PATH` remains an explicit operator override. Before the model loads, `ensure_cache_dir` creates the directory with `mode=0o700` and re-applies `chmod 0700` (so a pre-existing loose directory is tightened); the parent `.../strand` dir is `0700` via `perms.secure_parent_dir`. `strand doctor` inspects this directory and reports its path whether cached or not. `strand.service` adds `%h/.cache/strand` to `ReadWritePaths`, so weights persist across restarts (independent of `PrivateTmp=true`). No schema changes.

### `BUG-UI-001`: Input Dock Premature Wrap & Row-Height Jitter — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml` (`inputDock`, `messageInput`, `modelWatermark`)
* **Resolution:** Collapsed `messageInput.rightPadding` to `14px` when text is present so typing uses the full dock width. Dock width stabilization removes the animation-wrap race condition.

### `OBS-UI-002`: Transcript Presentation (Unboxed Text vs. Cards) — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml`
* **Resolution:** Assistant turns render as structured, recessed cards (`#13171f`, `1px` border, `radius: 12`) matching the conversational rhythm of user bubbles.

### `OBS-UI-006`: Diagnostics Slide-Out Legibility & Canvas Interference — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml` (`telemetryDrawer`)
* **Resolution:** Retitled drawer header to "TELEMETRY", increased background fill opacity to `0.96` (`#0d1117`) to eliminate text bleed-through, and resolved margin collisions.

### `OBS-POL-004` & `OBS-POL-006`: Operator Identity Stance & Day Zero Trapping — **Resolved**
* **Component:** `src/strand/extractors.py`, `proxy.py`, `onboarding.py`
* **Resolution:** Removed procedural regex name heuristics. Turn 1 opens with *"Hi, I'm Strand. What should I call you?"* without reflexively assuming `$USER`; if the operator is coy or indifferent ("whatever", "guess"), Strand may offer a cleaned-up name derived from the account name and ask if it works, so the conversation never stalls. Model B semantically identifies the operator name, tags it as `person` / `user_explicit` / `1.0`, binds the `operates_host` edge, and merges the generic `User` node and its edges into the operator.
* **Integration note (SEC-PRIV-002):** Day-Zero onboarding prompts can now casually offer the operator an opt-in question (*"Should I check out your machine specs, or keep that strictly on-demand?"* (preceded by "I keep things private by default—I don't review your local computer's details unless you want me to.")). A yes toggles `session.allow_host_telemetry` for the live session and persists `enable_host_telemetry: True`; otherwise only the hostname is shared and the operator anchor is unaffected.
* **Telemetry gate wired (OBS-POL-004 / OBS-POL-005 / SEC-PRIV-002):** `onboarding_directive(..., turn, telemetry_decision)` keeps Turns 1-3 free of hardware talk (Turn 1 opening line only; Turn 2/3 greeting, "Looks like I'm running locally on <Hostname>", the two-sentence mission and the domain-agnostic "What do you usually find yourself working on, creating, or exploring at this computer?"). On Turn 4/5 it adds the telemetry gate question (`TELEMETRY_GATE_QUESTION`) with graceful opt-in / opt-out handling (`TELEMETRY_DECLINED_ACK`), and stops asking once resolved. Model B distills the answer as `(<Operator>) --[prefers]--> (Host Telemetry Enabled | Host Telemetry On-Demand)` (`archetype: preference`; it never deprecates unrelated directives, traits or person attributes, only the opposite telemetry edge on a reversal). After distillation the proxy reads that edge (`onboarding.telemetry_preference`), calls `SessionLedger.set_allow_host_telemetry(session_id, choice)` so the very next turn carries (or withholds) the specs, and persists `enable_host_telemetry` with `save_config_value` (atomic, `0600`). No schema changes. Tests: `tests/test_onboarding.py`, `tests/test_proxy.py`, `tests/test_extractors.py`.

### `OBS-MEM-006`: False Edge Deprecations on Historical Items — **Resolved**
* **Component:** `src/strand/extractors.py`, `resolver.py`, `db.py`
* **Resolution:** Distillation instructions now preserve chronological statements (e.g., prior vehicle ownership) as active `previously_owned` edges instead of marking them as deprecated. Added collection query expansions in `resolver.py` and a `strand revive-vehicles` repair command in `db.py`.
