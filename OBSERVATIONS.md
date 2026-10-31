# Strand UX & Ergonomics Observations Log

This document tracks real-world friction points, visual bugs, and design observations captured during daily dogfooding sessions on ONYX.

---

## 1. Active Bugs (Triaged)

### `BUG-UI-001` (RESOLVED): Input Dock Premature Wrap & Row-Height Jitter
* **Component:** `src/strand/ui/qml/main.qml` (`inputDock`, `messageInput`, `modelWatermark`)
* **Symptom:** When typing continuously at ~60–70 WPM, as the cursor approaches the middle-right of the box, the input dock violently jitters between 1 and 2 rows on every keystroke. Once pushed fully to a second line, jitter stops.
* **Root Cause:** 
  1. `modelWatermark` fades its opacity to `0` upon typing, but `TextArea.rightPadding: modelWatermark.width + 20` remains permanently allocated (~120px void on the right).
  2. The text engine hits this phantom padding and wraps down to row 2.
  3. `Behavior on width` expands the container to fit the text, causing it to unwrap back to row 1, creating a race condition between the instantaneous wrap engine and the 150ms animation curve.
* **Status:** Resolved.
* **Resolution:**
  * `messageInput.rightPadding` is now `text.length > 0 ? 14 : modelWatermark.width + 20`: the watermark reservation applies only while the input is empty (baseline alignment and right padding preserved), and typed text gets the full dock width before wrapping.
  * `Behavior on width` on the dock is enabled only while the input is empty, so the width snaps while typing and the wrap engine never races a 150 ms animation.

---

## 2. Ergonomics & Presentation Explorations

### `OBS-UI-002`: Monolithic Narrative Density vs. Conversational Bubbles
* **Status:** Resolved. Assistant turns render as slate cards (`#13171f`, 1px `rgba(255,255,255,0.06)` border, radius 12), left-aligned, max width `min(85%, 820)`; the thinking state stays a compact ellipsis pill; Markdown and code cards render inside the card; `bubble.width` stays one-way.
* **Component:** `src/strand/ui/qml/main.qml` (Chat Transcript Delegates)
* **Symptom:** Asymmetric formatting (right-aligned user bubbles vs. 40+ lines of left-aligned unboxed text) creates a "wall of text" for multi-section responses (e.g., car comparisons, ranked specs). The conversational boundary and dialogue rhythm get lost.
* **Exploration Paths:**
  1. **Segmented Assistant Cards:** Re-introduce structured chat bubbles/cards for Strand turns (e.g., deep slate `#13171f` with subtle borders) to match the conversational rhythm of the user bubble.
  2. **Hybrid Formatting:** Render conversational preambles and closing verdicts as lightweight dialogue, while nesting numbered/bulleted comparison specs inside distinct recessed report cards.

### `OBS-ERG-003`: Stream Throughput Pacing & Perception Ergonomics
* **Component:** `src/strand/ui/bridge.py` & `main.qml`
* **Symptom:** Flash-Lite delivers responses at 300–400 t/s, snapping an entire multi-paragraph response onto the screen in under a second. The instant dump feels visually jarring and makes it hard to perceive if Strand actually grounded the prompt in local memory.
* **Exploration Paths:**
  1. **Client-Side Pacing Throttle:** Buffer incoming SSE chunks in `bridge.py` and release them into the QML transcript at a steady, human-readable rate (~60–80 t/s) with a subtle typing indicator.
  2. **Grounding Transparency:** Briefly illuminate the traversed nodes or delta stats in the header pill for 150ms before releasing the stream so local graph retrieval is visually perceptible.

### `OBS-UI-006`: Diagnostics Slide-Out Legibility & Canvas Interference
* **Status:** Resolved. Header is "TELEMETRY"; drawer fill is `#0d1117` at 0.96 opacity with a `rgba(255,255,255,0.08)` hairline border (no text ghosting); the panel floats 12px from the right edge and is vertically centred at 70% height.
* **Component:** `src/strand/ui/qml/main.qml` (`telemetryDrawer`, `drawerDismiss`)
* **Symptom:** The 280px drawer overlay exhibits readability, branding, and interaction defects:
  1. Title reads "FIBER TELEMETRY" which introduces forced metaphor overload. Should simply be "TELEMETRY".
  2. High transparency / weak background scrim lets dense transcript text show through, making diagnostic metrics and paths difficult to read.
  3. The drawer directly overlaps and occludes right-aligned user prompt bubbles when open.
  4. Vertical breathing room is cramped; the panel hugs the vertical bounds without margin relative to the window frame.
* **Refined Design Direction:**
  * Retitle header from "FIBER TELEMETRY" to "TELEMETRY".
  * Increase panel surface opacity (from 82% to 94–96% dark slate `#0d1117` or add a subtle backdrop blur `FastBlur` / `MultiEffect`) to eliminate text ghosting.
  * Adjust right margin/offset or transcript margins so active bubbles have breathing room.
  * In the upcoming transcript card refactor (`OBS-UI-002`), ensure chat cards don't collide visually with the open drawer overlay.

---

## 3. Onboarding & Policy Refinements

### `OBS-POL-004`: User Identity Stance (Display Name vs. Account `$USER`) — **Resolved**
* **Component:** `src/strand/proxy.py` (Onboarding Mode / Host Specs)
* **Symptom:** Assuming the Linux account username (`$USER` / `nic`) is the user's preferred conversational name creates an impersonal tone.
* **Refined Policy:**
  * Retain machine specs, hostname, and account username silently detected as ambient hardware context.
  * In the Day Zero opening directive, explicitly ask the operator for their preferred name:
    > *"Hi, I'm Strand. I'm here to provide local, sovereign digital continuity for your workstation. What should I call you?"*
  * Anchor the response as the primary `person` entity with explicit user provenance (`confidence: 1.0`).
* **Resolution:**
  * **True Day Zero:** onboarding activates when the DB holds 0 `person` entities with `origin == "user_explicit"` (model-inferred people no longer suppress it).
  * **Conversational flow:** Turn 1 opens with exactly "Hi, I'm Strand. What should I call you?" and the model may never use `$USER`. Later turns greet by the given name, acknowledge the local hostname, state the two-sentence sovereign-memory mission, and drift naturally through projects/stack and communication rules. Host specs stay ambient.
  * **Intent triage:** `/onboard` (`X-Strand-Mode: onboarding`) with an explicit operator on record offers: 1) update name/identity, 2) recalibrate directives/tone/formatting, 3) re-run full onboarding; a full wipe/reset is pointed at Settings. Updates are applied without data loss.
  * **Provenance:** Model B (`LLMExtractor`) turns "Call me Nic"/"I'm Nic" into a `person` (`user_explicit`, `1.0`) linked `--[operates_host]--> (hostname)`; edges of the generic `User` are re-pointed to it, and `reanchor_generic_user` folds a stored `User` node into the operator (alias `User`) after distillation. `/onboard` no longer seeds a `$USER`-named node.

### `OBS-POL-006`: Brittle Operator Name Extraction Trapped Users in Day Zero — **Resolved**
* **Component:** `src/strand/extractors.py`, `onboarding.py`, `proxy.py`
* **Symptom:** Answering "What should I call you?" with "Nic", "Just Nic" or "Nic works" matched no regex ("call me"/"I'm"/"my name is"), so no `user_explicit` operator was created, `count_operators()` stayed 0 and Day Zero repeated on every launch.
* **Resolution (final, supersedes earlier regex/heuristic iterations):** procedural name heuristics were deleted: `parse_name_answer`, `declared_name`, `anchor_declared_operator`, the `?` rejection, the 6-word ceiling, `EXPECTING_NAME`/`expecting_name` session flags, the proxy interceptor and `onboarding.anchor_operator_name`. Parsing natural dialogue with string rules was brittle; Strand already has Model A (conversational director) and Model B (semantic distillation).
  * Model A: the onboarding directive opens Turn 1 with "Hi, I'm Strand. What should I call you?" (no `$USER` fallback); later turns are unhurried peer dialogue on workbench focus, tech stack and communication stances.
  * Model B: `SYSTEM_PROMPT` tells it to emit the confirmed name ("Nic", "Just Nic", "Nic works", "Nicholas is fine") as `person` / `user_explicit` / `1.0` plus `(Name) --[operates_host]--> (Hostname)`. `parse_extraction` keeps that provenance only for `person` entities and their `operates_host` edge.
  * `reanchor_generic_user` still runs after distillation and folds a generic `User` node and its edges into the operator.
  * Trade-off: if Model B is unavailable or declines to emit the operator, Day Zero stays open until a later exchange declares one (fail-safe, no guessed names).
  * Tests (offline, mocked Model B): `tests/test_extractors.py`, `tests/test_proxy.py`, `tests/test_onboarding.py`.

### `OBS-POL-005`: Epistemic Evolution & Dynamic Persona Anchor
* **Component:** `src/strand/proxy.py`, `distill.py`, `compactor.py` (Personality Matrix)
* **Symptom:** A static `persona_prompt` in `config.json` is rigid and risks permanent drift from the operator's actual tone and cadence over weeks of use.
* **Refined Policy:**
  * **Ephemeral Bootstrap Umbilical:** The factory `persona_prompt` serves only as a transient anchor for the first few turns of Day Zero before being replaced by dynamically distilled nodes.
  * **Dynamic Persona Seeding:** Onboarding conversation organically seeds the foundational `trait`, `directive`, and `preference` nodes anchored to the operator.
  * **Continuous Evolution Loop:**
    * When communication styles shift in chat, Model B detects the critique and deprecates old edges (`weight: 0.0`, `metadata.deprecated: true`) in favor of new directives.
    * Periodic background compaction (`compactor.py`) evaluates multi-episode horizons to refine the operator profile and adjust active trait weights.
    * Inactive or transient behavioral nuances fade naturally via temporal half-life decay.
  * **Phase 1 status: Verified.** Model B's `SYSTEM_PROMPT` now carries a permanence-marker guardrail (permanent directives vs. transient steering). Permanent corrections emit `deprecated_edges` (applied via `db.deprecate_edge`: `weight 0.0`, `metadata.deprecated: true`, idempotent, nodes preserved including `user_explicit`) plus an active `directive`/`preference` replacement. Tests (offline, mocked): `tests/test_extractors.py`, `tests/test_distill.py`. Phases 2+ (compaction-driven profile refinement) remain open.
