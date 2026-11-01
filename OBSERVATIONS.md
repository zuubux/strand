# Strand UX & Ergonomics Observations Log

This document tracks real-world friction points, visual bugs, design observations, and security/packaging findings captured during dogfooding sessions on a development workstation.

---

## 1. Active Issues & Pre-Publishing Blockers (Triaged)

### `SEC-LOG-004`: Cleartext Entity and Deprecation Leaks at `INFO` Verbosity — **Resolved**
* **Status:** Resolved. Per-entity and per-edge traces in `distill.py` (alias merge, consolidation, user-directed forgetting, edge-free inferred drops, `Deprecated edge ... reason=...`) and the `already deprecated` trace in `db.deprecate_edge` are now `logger.debug`. At `INFO`, distillation logs only session ids and aggregate counts (`Recorded episode id=... session=... entities=N edges=N deprecated=N [forgotten=N]`, plus the substantive/skipped gate lines). Tests: `tests/test_distill.py`, `tests/test_config_logging.py`; `tests/test_deprecation.py` and `tests/test_forgetting.py` now capture at `DEBUG` for the traces. No schema change.
* **Component:** `src/strand/distill.py` (`process_exchange`, `_prune_deprecated_edges`)
* **Symptom:** Matched entity names, relationship triples, and deprecation rationales are logged at `INFO` level. If an operator copies systemd journal output (`journalctl --user -u strand`) or terminal logs into a public bug tracker or forum, their personal entity graph is exposed.
* **Root Cause:** Verbose pipeline tracing retained at standard production log levels.
* **Resolution Plan:**
  * Demote entity-level extraction logs, triple mappings, and deprecation details from `logger.info()` to `logger.debug()`.
  * Ensure production `INFO` logging tracks strictly operational counts and session identifiers (e.g., `entities=2 edges=1`).

### `SEC-CLI-005`: Plaintext Embedded Credentials in Diagnostic Commands (Resolved)
* **Status:** Resolved. `cli.mask_url` splits the URL with `urllib.parse.urlsplit` and rewrites the user-info as `user:********` (or `********` for a bare token). `strand config show` masks `upstream_url` and `extractor_url`; `strand doctor` masks the extractor URL and prints a masked upstream line. `strand config get <key>` stays raw by design. Unparseable input never raises.
* **Component:** `src/strand/cli.py` (`cmd_doctor`, `cmd_config_show`)
* **Symptom:** Running `strand doctor` or `strand config show` prints `upstream_url` and `extractor_url` directly to stdout. If an operator configures HTTP basic authentication (e.g., `https://user:pass@proxy.internal/v1`), credentials appear unmasked in terminal output, screenshots, and shell recordings.
* **Root Cause:** URLs are output verbatim without splitting the user-info component.
* **Resolution Plan:**
  * Parse URLs through `urllib.parse.urlsplit` in diagnostic views.
  * Mask password or token components (e.g., `https://user:••••••••@proxy.internal/v1`) before rendering to stdout.

### `SEC-NET-006`: Unauthenticated Non-Loopback TCP Startup — **Resolved**
* **Status:** Resolved. Fail-closed TCP policy: `daemon.enforce_bind_policy` raises `RuntimeError("Refusing to bind unauthenticated TCP on non-loopback interface ...")` when the transport is TCP, the host is not loopback and `api_key` is empty; `_run` logs a `SECURITY` error and exits via `SystemExit` before `build_app`. Override with `--insecure-allow-unauthenticated` or `STRAND_INSECURE_ALLOW_UNAUTHENTICATED=1|true|yes`: the loud `SECURITY` warning is logged and the server boots. UDS mode and loopback TCP (`127.*`, `localhost`, `::1`) are unaffected. No schema change. Tests: `tests/test_final_hardening.py`.
* **Component:** `src/strand/daemon.py` (`warn_if_exposed`, `main`)
* **Symptom:** When configured in TCP mode bound to an external or wildcard address (e.g., `0.0.0.0`), the daemon logs a security warning if `api_key` is blank, but still starts the server. This exposes the tool runner, local file inspection, and cognitive graph to any host on the local network without authentication.
* **Root Cause:** Permissive warn-only fallback for non-loopback bindings.
* **Resolution Plan:**
  * If binding beyond loopback (`ipaddress.is_loopback` is False) with no `api_key` configured, refuse to start by default.
  * Require an explicit CLI override (`--insecure-allow-unauthenticated`) to boot in open TCP mode.

### `BUG-CLI-007`: Unhandled Traceback on Corrupt Node Metadata — **Resolved**
* **Status:** Resolved. `cmd_inspect` now loads metadata through `common.load_metadata` (malformed JSON or non-object values yield `{}`), so aliases are never read from bad data. When the stored metadata is unparseable or not a JSON object, the `metadata:` line prints `{corrupted}` and the rest of the record (id, name, archetype, origin, confidence, summary, edges) renders normally with exit code 0. Valid metadata is unchanged. No schema change. Tests: `tests/test_cli.py`.
* **Component:** `src/strand/cli.py` (`cmd_inspect`)
* **Symptom:** Running `strand inspect <id>` on an entity whose metadata contains non-object or corrupt JSON causes a crash with an unhandled Python `json.decoder.JSONDecodeError` traceback.
* **Root Cause:** Direct invocation of `json.loads()` instead of using the resilient `common.load_metadata()` helper.
* **Resolution Plan:**
  * Replace `json.loads` in `cmd_inspect` with `common.load_metadata`.
  * Display a clean formatted fallback (`metadata: {corrupted}`) or graceful CLI warning rather than terminating with an unhandled exception.

### `SEC-CONF-008`: Short Secret Masking Information Leak — **Resolved**
* **Resolution:** `mask_secret` now fully masks any non-empty secret shorter than 16 characters as `********`; secrets of 16+ characters keep the first 3 and last 4 characters (`sk-...abcd`); empty/`None` returns `""`. Tests: `tests/test_config_logging.py`. Note: the function lives in `src/strand/cli.py`, not `config.py`.
* **Component:** `src/strand/cli.py` (`mask_secret`)
* **Symptom:** `mask_secret` retains the first and last few characters of any credential. For short bearer tokens or passwords (<16 chars), this exposes 7 out of 9 characters (e.g., `abc...fghi`), making short secrets trivial to reconstruct.
* **Root Cause:** Fixed slice indexing without length threshold branching.
* **Resolution Plan:**
  * If the string length is under 16 characters, mask the credential entirely with a fixed string (`********`).
  * Preserve head/tail reveals only for long tokens (>=16 chars) where entropy remains protected.

### `OBS-POL-005`: Dynamic Persona Refinement & Long-Term Compaction (Phase 2)
* **Component:** `src/strand/proxy.py`, `distill.py`, `compactor.py`
* **Status:** Phase 1 complete (bedrock anchor, dynamic graph precedence, critique deprecation with permanence markers).
* **Open Scope:** Phase 2 multi-episode compaction loop. Periodic synthesis should analyze multi-week conversational patterns, refine the operator profile, and gracefully decay inactive behavioral quirks.

---

## 2. Resolved Observations & Regressions

### `PUB-INST-003`: Phase 3 Automated Linux Installer — **Resolved**
* **Component:** `scripts/install.sh`, `README.md`
* **Resolution:** Added an idempotent per-user installer: it exits 1 when run as root, verifies Python >= 3.11, `systemd --user` and `pipx` (with `dnf`/`apt`/`pacman` hints), pre-creates the Strand and XDG directories `0700` (avoids systemd `226/NAMESPACE`), runs `pipx install --force [--editable] .`, installs the desktop entry, icon and `strand.service` (ExecStart rewritten to `%h/.local/bin/strand daemon` because a pipx venv is invisible to `/usr/bin/env python3`), enables the service, warms the FastEmbed cache and runs `strand doctor`. No schema change; no new dependencies.

### `PUB-PKG-002`: Desktop Packaging, GUI Decoupling & Docker Cleanup — **Resolved**
* **Component:** `packaging/`, `src/strand/ui/app.py`
* **Resolution:** Deleted the orphaned `packaging/docker/Dockerfile` (charter drift and an unauthenticated `0.0.0.0` reference) and scrubbed Docker mentions from `ARCHITECTURE.md`. Added `packaging/desktop/strand.desktop` and `packaging/icons/strand.svg`. `app.ensure_stdio()` makes `Terminal=false` launches safe when stdio is closed, and `main` returns an int exit code after `Bridge.shutdown()` joins all workers. No schema change.

### `PUB-SAN-001`: Phase 1 Repository, Identity & Environment Sanitization Sweep — **Resolved**
* **Component:** `src/strand/`, `tests/`, `packaging/systemd/strand.service`, docs
* **Resolution:** Operator-specific names, homelab hostnames, home-directory paths and personal vehicle fixtures were replaced with neutral values (`Alice`, `Bob`, `workstation-01`, `Acme Sedan`, `Cruiser`, `Kestrel`, `W34`, `X60`). `strand.service` now uses `ExecStart=/usr/bin/env python3 -m strand.daemon` with `%h` specifiers only. `host.py` and `onboarding.py` derive identity from `getpass.getuser()`/`$USER`/`platform.node()` and fall back to `Operator`/`workstation`. No schema change; 683 offline tests pass under `-W error`.

### `SEC-PERM-001`: Indiscriminate Parent Directory `chmod 0700` — **Resolved**
* **Component:** `src/strand/db.py`, `daemon.py`, `config.py` (`DatabaseManager.connect`, `prepare_socket`, `save_config_value`)
* **Resolution:** Added `perms.py` with `secure_parent_dir()`. Permissions are tightened strictly on directories named `strand` (and not `$HOME`). Unmanaged parents retain existing permissions with a warning emitted, while leaf files and sockets enforce `0600`.

### `SEC-PRIV-003`: Resolver Internal API Default Parameter Boundary — **Resolved**
* **Component:** `src/strand/resolver.py` (`match_entities`, `gather`, `_gather_with_roots`, `recent_continuity`, `personality_entities`)
* **Resolution:** Every resolver entry point now defaults to `allow_private=False` (fail-closed); `resolve()` still passes the flag explicitly. Existing tests that exercise private entities already passed `allow_private=True` explicitly, so none needed changes. Added `test_helpers_fail_closed_without_allow_private` in `tests/test_resolver_vector.py`, asserting that omitting the argument hides private entities and their edges. No schema change.

### `SEC-PRIV-002`: Day-Zero Host Hardware Fingerprinting — **Resolved**
* **Component:** `src/strand/proxy.py`, `host.py`, `config.py`, `session.py`, `ui/bridge.py`
* **Resolution:** `Config.enable_host_telemetry` defaults to `False`. When disabled, `host.redact_host_context` strips OS, CPU, RAM, and desktop specs, retaining only the hostname for `<Operator> --[operates_host]--> <Hostname>` topological anchoring. Outbound prompts include hardware telemetry only if explicitly opted into via settings or the `X-Strand-Host-Telemetry` header.

### `SEC-CACHE-003`: Shared `/tmp/fastembed_cache` Isolation — **Resolved**
* **Component:** `src/strand/embed.py` (`EmbeddingManager`), `packaging/systemd/strand.service`
* **Resolution:** `EmbeddingManager` routes weights to `~/.cache/strand/fastembed` (or `$XDG_CACHE_HOME`) with explicit `0700` POSIX permissions. Added `%h/.cache/strand` to `ReadWritePaths` in `strand.service`.

### `ERG-PATH-004`: Relative Path Sandbox Resolution Under `systemd --user` — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`is_safe_path`, `read_file`)
* **Resolution:** In `is_safe_path()`, candidate relative paths are evaluated across all declared directories in `config.workspace_roots` in addition to `os.getcwd()`, preventing false-positive 403 sandbox rejections when running under `systemd --user` with `$HOME` as the working directory.

### `BUG-UI-001`: Input Dock Premature Wrap & Row-Height Jitter — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml` (`inputDock`, `messageInput`, `modelWatermark`)
* **Resolution:** Collapsed `messageInput.rightPadding` to `14px` when text is present. Stabilized dock width to a centered container (`min(parent.width - 96, 760)`) with smooth vertical expansion.

### `OBS-UI-002`: Transcript Presentation (Unboxed Text vs. Cards) — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml`
* **Resolution:** Conversational text wraps into adaptive assistant bubbles, while code blocks and tables expand into full technical slate cards (`#0c0f14`), unified under a centralized theme object.

### `OBS-ERG-003`: Stream Throughput Pacing & Grounding Transparency — **Resolved**
* **Component:** `src/strand/ui/bridge.py` & `main.qml`
* **Resolution:** Client-side token pacing implemented at ~67 tokens/sec with instant Tab-key flush. Added the `X-Strand-Grounding` ~150 ms header pip pulse to visually signify memory retrieval before stream rendering begins.

### `OBS-UI-006`: Diagnostics Slide-Out Legibility & Canvas Interference — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml` (`telemetryDrawer`)
* **Resolution:** Retitled drawer header to "TELEMETRY", increased background fill opacity to `0.96` (`#0d1117`) to eliminate text bleed-through, and resolved margin collisions.

### `OBS-POL-004` & `OBS-POL-006`: Operator Identity Stance & Day Zero Anchoring — **Resolved**
* **Component:** `src/strand/extractors.py`, `proxy.py`, `onboarding.py`
* **Resolution:** Removed procedural regex name heuristics. Turn 1 prompts naturally without assuming `$USER`. Model B semantically identifies the operator name, tags it as `person` / `user_explicit` / `1.0`, binds the `operates_host` edge, and merges the generic `User` node and its edges into the operator.

### `OBS-MEM-006`: False Edge Deprecations on Historical Items — **Resolved**
* **Component:** `src/strand/extractors.py`, `resolver.py`, `db.py`
* **Resolution:** Distillation instructions preserve chronological statements (e.g., prior vehicle ownership) as active `previously_owned` edges instead of marking them as deprecated. User first-person assertions receive `user_explicit` / `1.0` provenance, and archetype collection queries traverse active history edges.