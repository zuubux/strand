"""QObject bridge between the QML chat window and the local Strand daemon.

Transport is the daemon's UNIX socket (loopback TCP fallback) via httpx. Each turn
streams on a worker thread with its own asyncio loop; Qt signals emitted there are
delivered to the GUI thread as queued connections.
"""

from __future__ import annotations

import asyncio
import codecs
import getpass
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from strand.config import (
    Config,
    _as_bool,
    _read_file,
    _validate_half_life,
    default_config_path,
    engine_updates,
    is_engine_configured,
    load_config,
    save_config,
)
from strand.db import _ACTIVE, PRIVATE_ENTITY_SQL, DatabaseManager
from strand.host import get_host_context
from strand.perms import secure_parent_dir
from strand.tools.workspace import IMAGE_MIME_TYPES, MAX_BATCH_FILE_BYTES, is_safe_path
from strand.onboarding import seed_operator_anchor
from strand.onboarding import operator_name as anchor_operator_name

DEFAULT_MODEL = "gpt-4o-mini"
UDS_BASE_URL = "http://strand"
CHAT_PATH = "/v1/chat/completions"
PRIVATE_HEADER = "X-Strand-Allow-Private"
HISTORY_LIMIT = 100
IMAGE_TOKEN_ESTIMATE = 1600
EXTRACTOR_TIMEOUT = 1.5
MODEL_PROBE_TIMEOUT = 3.0
EXTRACTOR_TAGS_TIMEOUT = 2.0
NON_CHAT_MODEL_RE = re.compile(r"embed|bge|whisper|tts|dall-e|moderation", re.IGNORECASE)
# Keys the Settings modal may persist to config.json, with their value kind.
SETTINGS_KEYS: dict[str, str] = {
    "persona_prompt": "str",
    "upstream_url": "str",
    "upstream_api_key": "secret",
    "model_default": "str",
    "extractor_url": "str",
    "extractor_model": "str",
    "decay_enabled": "bool",
    "decay_half_life_days": "float",
    "redact_secrets": "bool",
    "enable_host_telemetry": "bool",
    "web_search_enabled": "bool",
}
# Client-side presentation pacing (OBS-ERG-003): the socket is never throttled, only the
# drain of the in-memory buffer into the transcript.
PACE_TOKENS_PER_SEC = 67.0  # ~65-70 t/s (~270 chars/s): ~1.07 token (~4-5 chars) credit per 16 ms tick
PACE_INTERVAL_MS = 16
CHARS_PER_TOKEN = 4
GROUNDING_HEADER_NAME = "X-Strand-Grounding"
MUTATIONS_HEADER_NAME = "X-Strand-Mutations"
RESTORE_PATH = "/v1/backups/restore"
RESTORE_TIMEOUT = 10.0
MUTATION_OPS = ("created", "modified", "patched", "moved", "removed")
MAX_MUTATION_RECORDS = 20
GROUNDING_STRIKE_MS = 150  # transient submit-to-first-chunk flash (3-beat conduit, beat 1)
DISTILL_HOLD_MS = 1800  # Model B weave window shown after a stream completes (beat 3)
GROUNDING_HOLD_S = 0.15  # grounded turns: the pulse is perceptible before the first token
GROUNDING_RE = re.compile(r"(\w+)=(\d+)")
TABLE_LINE_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
KV_LINE_RE = re.compile(r"^[ \t]*[A-Za-z_][\w.-]*[ \t]*(?::|=)[ \t]+\S")
PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n")
COMMAND_RE = re.compile(r"^/([A-Za-z]+)(?:[ \t]+(.*))?$", re.DOTALL)
FENCE_RE = re.compile(r"^[ \t]{0,3}```[ \t]*([\w+.#-]*)[ \t]*$")
# Markdown images (`![alt](url)`, `![alt][ref]`, `![alt]`): Qt Quick fetches these over the network.
IMAGE_RE = re.compile(r"!\[([^\]]*)\](?:\((?:[^()]|\([^()]*\))*\)|\[[^\]]*\])?")
HELP_TEXT = (
    "Commands: /new (fresh session), /private on|off (send X-Strand-Allow-Private), "
    "/model <name> (switch model), /help. Ctrl+L new session, Ctrl+D diagnostics, Esc stop/clear."
)


def parse_model_names(payload: Any, ollama: bool = False) -> list[str]:
    """Chat-capable model ids from an OpenAI `/models` or Ollama `/api/tags` body."""
    if ollama:
        items, keys = payload.get("models", []), ("name", "model")
    else:
        items, keys = payload.get("data", []), ("id", "name")
    names: list[str] = []
    for item in items:
        name = next((str(item[k]).strip() for k in keys if item.get(k)), "")
        if name and not NON_CHAT_MODEL_RE.search(name) and name not in names:
            names.append(name)
    return names


# Lightweight upstream models are recognised by pattern, never by a pinned version string.
LIGHTWEIGHT_KEYWORDS = ("lite", "flash", "mini", "haiku", "nano", "small")
_HEAVY_MODEL_WORDS = frozenset({"o1", "o3", "r1", "opus", "reasoning"})
_LOCAL_LIGHT_WORDS = frozenset({"3b", "7b", "14b", "small", "mini"})


def _model_words(model_id: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", model_id.lower()))


def _model_sort_key(model_id: str, default_model: str) -> tuple:
    """Sort key: pinned default, then `lite`, then other lightweight, standard, heavy/reasoning.

    Keywords are matched as whole tokens (so `gemini` is not `mini`); within a tier ids sort
    reverse-alphabetically, so newer versions come first.
    """
    words = _model_words(model_id)
    if model_id == default_model:
        tier = 0
    elif words & _HEAVY_MODEL_WORDS:
        tier = 4
    elif "lite" in words:
        tier = 1
    elif words & set(LIGHTWEIGHT_KEYWORDS):
        tier = 2
    else:
        tier = 3
    return (tier, tuple(-ord(c) for c in model_id) + (0,))


def pick_lightweight_model(models: list[str]) -> str:
    """First lightweight model in `models`, preferring `lite`, then `flash`; empty if none match."""
    for preferred in ("lite", "flash"):
        for model in models:
            if preferred in _model_words(model):
                return model
    for model in models:
        if _model_words(model) & set(LIGHTWEIGHT_KEYWORDS):
            return model
    return ""


def _extractor_sort_key(model_id: str) -> tuple:
    """Small local models (3b/7b/14b/small/mini) first, then alphabetical."""
    return (0 if _model_words(model_id) & _LOCAL_LIGHT_WORDS else 1, model_id)


def operator_name(env: Mapping[str, str] | None = None) -> str:
    """Operator display name: explicit env override, else the capitalised login name, else `Operator`."""
    env = os.environ if env is None else env
    name = (env.get("STRAND_UI_OPERATOR") or "").strip()
    if not name:
        try:
            name = getpass.getuser().strip()
        except Exception:
            name = ""
        if name.lower() in ("root", ""):
            name = ""
        name = name[:1].upper() + name[1:]
    return name or "Operator"


def _tilde(path: str) -> str:
    """Collapse the home directory prefix to `~` and drop a trailing slash."""
    home = str(Path.home())
    path = path.rstrip("/") or "/"
    if path == home:
        return "~"
    return "~" + path[len(home):] if path.startswith(home + "/") else path


DROPIN_MARKER = "# Managed by Strand setup"


def _write_systemd_dropin(roots: list[str]) -> Path:
    """Write `strand.service.d/override.conf` adding the workspace roots to `ReadWritePaths=`.

    The list directive appends to the packaged unit's paths. The `-` prefix keeps the service
    starting if a root is missing. A pre-existing override that Strand did not write is kept as
    `override.conf.strand-bak`.
    """
    path = Path.home() / ".config" / "systemd" / "user" / "strand.service.d" / "override.conf"
    entries = []
    for root in roots:
        token = "-" + str(Path(root).expanduser()).replace("%", "%%")
        entries.append(f'"{token}"' if " " in token else token)
    text = f"{DROPIN_MARKER}\n[Service]\nReadWritePaths={' '.join(entries)}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and DROPIN_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
        os.replace(path, path.with_name(path.name + ".strand-bak"))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".override-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def parse_command(text: str) -> tuple[str, str] | None:
    """Split a client-side slash command into `(name, argument)`, or None for normal prompts.

    Only a single leading `/word` counts, so paths such as `/etc/hosts` are sent upstream.
    """
    match = COMMAND_RE.match(text.strip())
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


def neutralize_images(text: str) -> str:
    """Replace Markdown image syntax with inert text so Qt Quick never fetches a remote URL.

    `![alt](url)` becomes `[image: alt]` (`[image]` without alt text). The URL is dropped.
    Regular links (`[label](url)`) are untouched.
    """

    def repl(match: re.Match[str]) -> str:
        alt = " ".join(match.group(1).split())
        return f"[image: {alt}]" if alt else "[image]"

    return IMAGE_RE.sub(repl, text)


def split_markdown(text: str) -> list[dict[str, Any]]:
    """Split text into `{code, lang, text}` segments on ``` fences.

    An unclosed fence (a stream still in progress) stays a code segment to the end.
    """
    segments: list[dict[str, Any]] = []
    buf: list[str] = []
    in_code = False
    lang = ""

    def flush() -> None:
        body = "\n".join(buf)
        if in_code or body.strip():
            segments.append({"code": in_code, "lang": lang if in_code else "", "text": body})
        buf.clear()

    for line in text.split("\n"):
        fence = FENCE_RE.match(line)
        if fence and not in_code:
            flush()
            in_code, lang = True, fence.group(1)
        elif fence and in_code and not fence.group(1):
            flush()
            in_code, lang = False, ""
        else:
            buf.append(line)
    flush()
    for segment in segments:
        if not segment["code"]:
            segment["text"] = neutralize_images(segment["text"])
    return segments


def split_blocks(text: str) -> list[dict[str, Any]]:
    """Presentation blocks `{kind, code, lang, text}` for an assistant reply.

    `prose` renders in a conversational bubble; `code` (fences), `table` (markdown pipe tables)
    and `data` (multi-line structured data) break out onto the canvas.
    """
    blocks: list[dict[str, Any]] = []

    def add(kind: str, body: str, lang: str = "") -> None:
        blocks.append({"kind": kind, "code": kind in ("code", "data"), "lang": lang, "text": body})

    for segment in split_markdown(text):
        if segment["code"]:
            add("code", segment["text"], segment["lang"])
            continue
        lines = segment["text"].split("\n")
        prose: list[str] = []

        def flush_prose() -> None:
            body = "\n".join(prose)
            # Blank-line separated paragraphs are individual conversational bubbles.
            for paragraph in PARAGRAPH_SPLIT_RE.split(body):
                if paragraph.strip():
                    add("prose", paragraph.strip("\n"))
            prose.clear()

        i = 0
        while i < len(lines):
            line = lines[i]
            run, kind, minimum = i, "", 0
            if TABLE_LINE_RE.match(line):
                while run < len(lines) and TABLE_LINE_RE.match(lines[run]):
                    run += 1
                kind, minimum = "table", 2
            elif KV_LINE_RE.match(line):
                while run < len(lines) and KV_LINE_RE.match(lines[run]):
                    run += 1
                kind, minimum = "data", 3
            elif line.strip() in ("{", "["):
                while run < len(lines) and lines[run].strip():
                    run += 1
                kind, minimum = "data", 3
            if kind and run - i >= minimum:
                flush_prose()
                add(kind, "\n".join(lines[i:run]), kind)
                i = run
            else:
                prose.append(line)
                i += 1
        flush_prose()
    return blocks


def parse_mutations(value: str | None) -> list[dict[str, str]]:
    """Parse the proxy's `X-Strand-Mutations` JSON header into `{path, op, backup_id}` records."""
    if not value:
        return []
    try:
        raw = json.loads(value)
    except ValueError:
        return []
    records: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        path, op, backup_id = item.get("path"), item.get("op"), item.get("backup_id")
        if isinstance(path, str) and isinstance(backup_id, str) and backup_id and op in MUTATION_OPS:
            records.append({"path": path, "op": op, "backup_id": backup_id})
        if len(records) >= MAX_MUTATION_RECORDS:
            break
    return records


def parse_grounding(value: str | None) -> dict[str, int]:
    """Parse the proxy's `entities=2 edges=1 ...` grounding header into counts."""
    return {key: int(num) for key, num in GROUNDING_RE.findall(value or "")}


def grounding_summary(counts: Mapping[str, int]) -> str:
    """`2 entities • 1 edges` style summary of the non-zero counts."""
    return " \u2022 ".join(f"{n} {key}" for key, n in counts.items() if n)


def resolve_model(
    config: Config, env: Mapping[str, str] | None = None, explicit: str | None = None
) -> str:
    """Pick the chat model: explicit > $STRAND_UI_MODEL > config `model_default` > fallback."""
    env = os.environ if env is None else env
    for candidate in (explicit, env.get("STRAND_UI_MODEL"), config.model_default):
        if candidate and candidate.strip():
            return candidate.strip()
    return DEFAULT_MODEL


def resolve_endpoint(config: Config, env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """Return `(uds_path, base_url)`. `uds_path` is None when the TCP fallback applies.

    The socket is used only when the daemon's socket path exists and is a socket.
    """
    path = config.effective_socket_path(env)
    if path:
        try:
            if stat.S_ISSOCK(os.stat(path).st_mode):
                return path, UDS_BASE_URL
        except OSError:
            pass
    return None, f"http://{config.host}:{config.port}"


def status_label(uds_path: str | None, base_url: str) -> str:
    return f"Connected: {os.path.basename(uds_path)}" if uds_path else f"Connected: {base_url[7:]}"


def parse_sse_line(line: str) -> tuple[str, str]:
    """Classify one SSE line as ("token", text), ("done", ""), ("error", msg) or ("skip", "")."""
    line = line.strip()
    if not line.startswith("data:"):
        return "skip", ""
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "done", ""
    try:
        data = json.loads(payload)
    except ValueError:
        return "skip", ""
    if not isinstance(data, dict):
        return "skip", ""
    err = data.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return "error", str(msg or "upstream error")
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta") or {}
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str) and content:
            return "token", content
    return "skip", ""


STANCE_LIMIT = 6  # active stance nodes mirrored in the Settings modal


class Bridge(QObject):
    chunkReceived = Signal(str)
    streamFinished = Signal()
    errorOccurred = Signal(str)
    statusChanged = Signal()
    busyChanged = Signal()
    modelChanged = Signal()
    sessionChanged = Signal()
    privateChanged = Signal()
    webSearchEnabledChanged = Signal()
    diagnosticsChanged = Signal()
    streamAborted = Signal()
    conversationReset = Signal()
    extractorStatusChanged = Signal(bool, str)
    groundingChanged = Signal()
    mutationsRecorded = Signal(str)
    backupReverted = Signal(str)
    revertedBackupsChanged = Signal()
    configuredChanged = Signal()
    operatorChanged = Signal()
    availableModelsChanged = Signal()
    availableExtractorModelsChanged = Signal()
    extractorProbeStatusChanged = Signal()
    _extractorModelsResult = Signal(int, bool, list)
    isProbingModelsChanged = Signal()
    probeStatusChanged = Signal()
    _probeResult = Signal(bool, list)
    distillingChanged = Signal()
    groundingActiveChanged = Signal()
    activeStanceNodesChanged = Signal()
    _wake = Signal()

    def __init__(
        self,
        config: Config | None = None,
        model: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        parent: QObject | None = None,
        probe_factory: Callable[[], httpx.AsyncClient] | None = None,
        pace_tps: float = PACE_TOKENS_PER_SEC,
    ) -> None:
        super().__init__(parent)
        # Pacing buffer: (generation, text) chunks drained by a GUI-thread timer.
        self._pace_tps = pace_tps
        self._buffer: deque[tuple[int, str]] = deque()
        self._end: tuple[int, Signal, tuple[Any, ...], bool] | None = None
        self._release_busy = False
        self._credit = 0.0
        self._hold_until = 0.0
        self._shown = False
        self._pulse = False
        self._grounding = ""
        self._timer = QTimer(self)
        self._timer.setInterval(PACE_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        # Conduit states: single-shot timers only; nothing runs while idle (OBS-ERG-003).
        self._distilling = False
        self._strike = False
        self._distill_timer = QTimer(self)
        self._distill_timer.setSingleShot(True)
        self._distill_timer.setInterval(DISTILL_HOLD_MS)
        self._distill_timer.timeout.connect(self._end_distilling)
        self._strike_timer = QTimer(self)
        self._strike_timer.setSingleShot(True)
        self._strike_timer.setInterval(GROUNDING_STRIKE_MS)
        self._strike_timer.timeout.connect(self._end_strike)
        self.streamFinished.connect(self._begin_distilling)
        self.streamAborted.connect(self._end_distilling)
        self._wake.connect(self._on_wake)
        self._probe_factory = probe_factory
        self._available_models: list[str] = []
        self._probing_models = False
        self._probe_status = "idle"
        self._probe_seq = 0
        self._probeResult.connect(self._on_probe_result)
        self._available_extractor_models: list[str] = []
        self._extractor_probe_status = "idle"
        self._extractor_probe_seq = 0
        self._extractorModelsResult.connect(self._on_extractor_models_result)
        self._config = config if config is not None else load_config()
        self._operator_override = str(_read_file(default_config_path()).get("operator_name") or "").strip()
        self._stance_nodes: list[str] = []
        self._stance_count = 0
        self._model = resolve_model(self._config, explicit=model)
        self._session_id = f"ui-{uuid.uuid4().hex[:12]}"
        self._uds_path, self._base_url = resolve_endpoint(self._config)
        self._client_factory = client_factory
        self._history: list[dict[str, str]] = []
        self._status = "Connecting…"
        self._busy = False
        self._lock = threading.Lock()
        self._private = False
        self._web_search_enabled = bool(getattr(self._config, "web_search_enabled", False))
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._abort_requested = False
        self._first_token_ms = 0
        self._token_count = 0
        self._prompts: list[str] = []
        self._prompt_index: int | None = None
        self._turn: dict[str, Any] = {}
        self._mutations: list[dict[str, str]] = []
        self._reverted: list[str] = []
        self._workers: dict[threading.Thread, tuple[asyncio.AbstractEventLoop, asyncio.Task[None]]] = {}

    @Property(list, notify=activeStanceNodesChanged)
    def activeStanceNodes(self) -> list[str]:
        """Top learned directive / preference / trait names (read-only mirror of memory.db)."""
        return list(self._stance_nodes or [])

    @Property(int, notify=activeStanceNodesChanged)
    def activeStanceCount(self) -> int:
        """Total active stance nodes in memory (the list above is capped)."""
        return int(self._stance_count or 0)

    @Slot()
    def refreshActiveStance(self) -> None:
        """Re-read active stance nodes from memory.db (read-only connection; never writes)."""
        nodes, count = self._read_stance()
        if nodes != self._stance_nodes or count != self._stance_count:
            self._stance_nodes, self._stance_count = nodes, count
            self.activeStanceNodesChanged.emit()

    def _read_stance(self) -> tuple[list[str], int]:
        path = Path(self._config.db_path).expanduser()
        if not path.is_file():
            return [], 0
        sql = (
            "SELECT t.name, MAX(e.weight) AS w, MAX(e.confidence) AS c FROM edges e "
            "JOIN entities t ON t.id = e.target_id "
            "WHERE t.archetype IN ('directive', 'preference', 'trait') "
            "AND " + _ACTIVE.format(t="e") + " AND NOT " + PRIVATE_ENTITY_SQL.format(t="t") + " "
            "GROUP BY t.id ORDER BY w DESC, c DESC, t.name ASC"
        )
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
            try:
                rows = [str(r[0]).strip() for r in conn.execute(sql).fetchall() if str(r[0]).strip()]
            finally:
                conn.close()
        except sqlite3.Error:
            return [], 0
        return rows[:STANCE_LIMIT], len(rows)

    # -- properties exposed to QML -------------------------------------------------
    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(str, notify=modelChanged)
    def modelName(self) -> str:
        return self._model

    @Property(str, notify=sessionChanged)
    def sessionId(self) -> str:
        return self._session_id

    @Property(bool, notify=privateChanged)
    def privateMode(self) -> bool:
        return self._private

    @Property(bool, notify=webSearchEnabledChanged)
    def webSearchEnabled(self) -> bool:
        return bool(self._web_search_enabled)

    @Slot(bool)
    def setWebSearchEnabled(self, enabled: bool) -> None:
        """Persist `web_search_enabled` (Google Search grounding for Gemini upstreams)."""
        enabled = bool(enabled)
        if self.saveConfig({"web_search_enabled": enabled}) and enabled != self._web_search_enabled:
            self._web_search_enabled = enabled
            self.webSearchEnabledChanged.emit()

    @Property(str, notify=operatorChanged)
    def operatorName(self) -> str:
        """Display name for the identity header: `$STRAND_UI_OPERATOR`, the name chosen in setup, login name, `Operator`."""
        if self._operator_override and not os.environ.get("STRAND_UI_OPERATOR", "").strip():
            return self._operator_override
        return operator_name()

    @Property(str, constant=True)
    def logoUrl(self) -> str:
        """file:// URL of the Strand logo for the Welcome wizard."""
        from PySide6.QtCore import QUrl

        from strand.ui.app import fallback_icon_path

        return QUrl.fromLocalFile(fallback_icon_path()).toString()

    @Property(str, constant=True)
    def hostName(self) -> str:
        return str(get_host_context().get("hostname") or "workstation")

    @Property(list, constant=True)
    def workspaceRoots(self) -> list[str]:
        """Currently configured workspace roots (default `~/Projects`)."""
        return list(self._config.workspace_roots)

    @Property(bool, constant=True)
    def hostTelemetryDefault(self) -> bool:
        return bool(self._config.enable_host_telemetry)

    @Property(str, constant=True)
    def transport(self) -> str:
        return f"unix:{self._uds_path}" if self._uds_path else f"tcp:{self._base_url[7:]}"

    def _transport_detail(self) -> str:
        """Active runtime path plus mode, e.g. `unix:/run/strand/strand.sock (0600)`."""
        if not self._uds_path:
            return f"{self.transport} (loopback)"
        try:
            mode = f"{stat.S_IMODE(os.stat(self._uds_path).st_mode):04o}"
        except OSError:
            mode = "unavailable"
        return f"{self.transport} ({mode})"

    @Property("QVariantMap", notify=diagnosticsChanged)
    def diagnostics(self) -> dict[str, Any]:
        """Session/model/transport plus metrics of the latest turn (empty values before one)."""
        info: dict[str, Any] = {
            "sessionId": self._session_id,
            "model": self._model,
            "transport": self._transport_detail(),
            "status": self._status,
            "private": self._private,
            "promptChars": 0,
            "replyChars": 0,
            "latencyMs": 0,
            "firstTokenMs": 0,
            "ttft_ms": 0,
            "total_ms": 0,
            "token_count": 0,
            "throughput": "0.0 t/s",
            "ledger": "Active" if self._history else "Cached",
            "outcome": "",
        }
        info.update(self._turn)
        return info

    @Property(list, notify=availableModelsChanged)
    def availableModels(self) -> list[str]:
        return list(self._available_models)

    @Property(list, notify=availableExtractorModelsChanged)
    def availableExtractorModels(self) -> list[str]:
        return list(self._available_extractor_models or [])

    @Property(str, notify=extractorProbeStatusChanged)
    def extractorProbeStatus(self) -> str:
        return self._extractor_probe_status or ""

    @Slot(result=str)
    def pickDefaultModel(self) -> str:
        """Lightweight model (preferring lite/flash) from the last upstream probe, or empty."""
        return pick_lightweight_model(self._available_models)

    @Property(bool, notify=isProbingModelsChanged)
    def isProbingModels(self) -> bool:
        return self._probing_models

    @Property(str, notify=probeStatusChanged)
    def probeStatus(self) -> str:
        return self._probe_status

    @Property(bool, notify=configuredChanged)
    def isConfigured(self) -> bool:
        """True when an upstream API key is set or the upstream is local Ollama."""
        return is_engine_configured(self._config)

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=distillingChanged)
    def distilling(self) -> bool:
        """True for ~1.8 s after a stream completes, while Model B weaves the turn into memory."""
        return self._distilling

    @Property(bool, notify=groundingActiveChanged)
    def grounding(self) -> bool:
        """Transient 150 ms strike when a message is submitted, before the first chunk arrives."""
        return self._strike

    @Slot()
    def _begin_distilling(self) -> None:
        self._set_distilling(True)
        self._distill_timer.start()

    @Slot()
    def _end_distilling(self) -> None:
        self._distill_timer.stop()
        self._set_distilling(False)

    def _set_distilling(self, value: bool) -> None:
        if value != self._distilling:
            self._distilling = value
            self.distillingChanged.emit()

    def _begin_strike(self) -> None:
        self._set_strike(True)
        self._strike_timer.start()

    @Slot()
    def _end_strike(self) -> None:
        self._strike_timer.stop()
        self._set_strike(False)

    def _set_strike(self, value: bool) -> None:
        if value != self._strike:
            self._strike = value
            self.groundingActiveChanged.emit()

    @Property(bool, notify=groundingChanged)
    def groundingPulse(self) -> bool:
        """True from the moment memory was retrieved until the first token is shown."""
        return self._pulse

    @Property(str, notify=groundingChanged)
    def groundingSummary(self) -> str:
        return self._grounding

    @property
    def buffered(self) -> int:
        """Chunks waiting in the pacing buffer."""
        return len(self._buffer)

    @property
    def history(self) -> list[dict[str, str]]:
        return list(self._history)

    # -- client-side stream pacing (OBS-ERG-003) ---------------------------------------
    def _set_grounding(self, pulse: bool, summary: str | None = None) -> None:
        summary = self._grounding if summary is None else summary
        if pulse != self._pulse or summary != self._grounding:
            self._pulse, self._grounding = pulse, summary
            self.groundingChanged.emit()

    def _note_mutations(self, value: str | None, generation: int) -> None:
        records = parse_mutations(value)
        if records and generation == self._generation:
            self._mutations = records
            self.mutationsRecorded.emit(json.dumps(records))

    @Property(list, notify=revertedBackupsChanged)
    def revertedBackups(self) -> list[str]:
        """Backup ids already rolled back this session (drives the pill's Reverted state)."""
        return list(self._reverted)

    @Slot(str, result=bool)
    def revertBackup(self, backup_id: str) -> bool:
        """Ask the daemon to restore a journaled mutation; True on success, False on any failure."""
        backup_id = backup_id.strip() if isinstance(backup_id, str) else ""
        if not backup_id:
            return False
        if backup_id in self._reverted:
            return True

        async def post() -> bool:
            async with self.make_client() as client:
                resp = await client.post(
                    RESTORE_PATH, json={"backup_id": backup_id}, headers=self._headers(), timeout=RESTORE_TIMEOUT
                )
                return resp.status_code == 200

        try:
            ok = asyncio.run(post())
        except Exception:  # noqa: BLE001 - a failed undo must never crash the UI
            ok = False
        if ok:
            self._reverted.append(backup_id)
            self.revertedBackupsChanged.emit()
            self.backupReverted.emit(backup_id)
        return ok

    @Slot(str, result="QVariantList")
    def mutationRecords(self, text: str) -> list[dict[str, str]]:
        """Decode a turn's JSON mutation payload for the QML pill row (never raises)."""
        try:
            return parse_mutations(text)
        except Exception:  # noqa: BLE001
            return []

    def _note_grounding(self, value: str | None) -> None:
        counts = parse_grounding(value)
        grounded = any(counts.values())
        if grounded:
            self._hold_until = time.monotonic() + GROUNDING_HOLD_S
        self._set_grounding(grounded, grounding_summary(counts))

    def _emit_text(self, text: str) -> None:
        self._shown = True
        self._end_strike()
        self._set_grounding(False)  # first visible token settles the pulse
        self.chunkReceived.emit(text)

    def _relay(self, generation: int, paced: bool, text: str) -> None:
        """Hand a socket chunk to the UI: straight through, or via the pacing buffer."""
        if paced and self._pace_tps > 0:
            self._buffer.append((generation, text))
            self._wake.emit()
        else:
            self._emit_text(text)

    def _deliver_end(
        self, generation: int, paced: bool, signal: Signal, *args: Any, immediate: bool = False
    ) -> None:
        """Emit a terminal signal, after the buffer drains when paced (immediately on error/abort)."""
        if paced and self._pace_tps > 0:
            with self._lock:
                self._end = (generation, signal, args, immediate)
            self._wake.emit()
        else:
            self._set_grounding(False)
            signal.emit(*args)

    def _turn_done(self) -> None:
        """Worker finished: release `busy` now, or once the paced buffer has drained."""
        with self._lock:
            if self._buffer or self._end is not None:
                self._release_busy = True
                return
            self._set_busy(False)

    @Slot()
    def _on_wake(self) -> None:
        if self._buffer and not self._timer.isActive():
            self._timer.start()
        if self._buffer and not self._shown:
            self._credit = max(self._credit, 1.0)  # the first token never waits for the timer
            text = self._drain_text(False)
            if text:
                self._emit_text(text)
        if not self._buffer:
            self._settle()

    def _drain_text(self, everything: bool) -> str:
        out: list[str] = []
        if not everything and time.monotonic() < self._hold_until:
            return ""
        while everything or self._credit > 0:
            try:
                generation, text = self._buffer.popleft()
            except IndexError:
                break
            if generation != self._generation:
                continue  # stale chunk of a reset conversation
            out.append(text)
            if not everything:
                self._credit -= max(1, -(-len(text) // CHARS_PER_TOKEN))
        return "".join(out)

    def _tick(self) -> None:
        self._credit += self._pace_tps * PACE_INTERVAL_MS / 1000
        text = self._drain_text(False)
        if text:
            self._emit_text(text)
        if not self._buffer:
            self._credit = min(self._credit, 0.0)  # no burst after an idle gap
        self._settle()

    def _settle(self) -> None:
        """Emit the pending terminal signal once the buffer is empty; then release `busy`."""
        with self._lock:
            end = self._end
            if self._buffer and not (end is not None and end[3]):
                return
            self._end = None
        if self._buffer:  # error/abort: show everything received so far at once
            text = self._drain_text(True)
            if text:
                self._emit_text(text)
        if end is not None and end[0] == self._generation:
            self._set_grounding(False)
            end[1].emit(*end[2])
        with self._lock:
            if self._release_busy and not self._buffer and self._end is None:
                self._release_busy = False
                self._set_busy(False)
            idle = not self._buffer and self._end is None
        if idle:
            self._timer.stop()
            self._credit = 0.0

    @Slot(result=int)
    def flush_stream(self) -> int:
        """Instantly drain the pacing buffer into the transcript (Tab fast-forward).

        Returns the number of characters released.
        """
        text = self._drain_text(True)
        if text:
            self._emit_text(text)
        self._settle()
        return len(text)

    # -- transport -------------------------------------------------------------------
    def make_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        timeout = httpx.Timeout(None, connect=5.0)
        if self._uds_path:
            transport = httpx.AsyncHTTPTransport(uds=self._uds_path)
            return httpx.AsyncClient(transport=transport, base_url=self._base_url, timeout=timeout)
        return httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "X-Session-Id": self._session_id}
        if self._private:
            headers[PRIVATE_HEADER] = "true"
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.statusChanged.emit()

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            self.busyChanged.emit()

    async def _check(self) -> None:
        try:
            async with self.make_client() as client:
                resp = await client.get("/health", timeout=3.0)
            ok = resp.status_code < 500
        except httpx.HTTPError:
            ok = False
        self._set_status(status_label(self._uds_path, self._base_url) if ok else "Disconnected")

    async def _stream(
        self, messages: list[dict[str, str]], generation: int | None = None, paced: bool = False
    ) -> None:
        """Stream one completion; emits chunk/finished/error/aborted signals, never raises httpx errors.

        `generation` ties the turn to the conversation it started in: if the conversation was
        reset meanwhile, history is left alone (the new session must stay clean). `paced` routes
        text through the client-side pacing buffer (the socket read itself is never throttled).
        """
        gen = self._generation if generation is None else generation
        self._shown = False
        self._set_grounding(False, "")
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "session_id": self._session_id,
        }
        parts: list[str] = []
        error: str | None = None
        aborted = False
        started = time.perf_counter()
        self._first_token_ms = 0
        self._token_count = 0
        self._mutations = []
        try:
            async with self.make_client() as client:
                async with client.stream("POST", CHAT_PATH, json=body, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace").strip()
                        error = f"HTTP {resp.status_code}: {detail[:300]}" if detail else f"HTTP {resp.status_code}"
                    else:
                        self._note_grounding(resp.headers.get(GROUNDING_HEADER_NAME))
                        self._note_mutations(resp.headers.get(MUTATIONS_HEADER_NAME), gen)
                        error = await self._consume(resp, parts, started, gen, paced)
                        self._set_status(status_label(self._uds_path, self._base_url))
        except asyncio.CancelledError:
            aborted = True
        except httpx.ConnectError as exc:
            self._set_status("Disconnected")
            error = f"Cannot reach Strand daemon: {exc}"
        except httpx.HTTPError as exc:
            error = f"Request failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - nothing may escape the worker thread
            if self._abort_requested:
                aborted = True  # teardown noise from a cancelled stream is silent
            else:
                error = f"Stream failed: {exc}"
        current = generation is None or generation == self._generation
        if current:
            total = time.perf_counter() - started
            ttft = self._first_token_ms / 1000
            tps = self._token_count / max(0.001, total - ttft)
            self._turn = {
                "throughput": f"{tps:.1f} t/s",
                "ttft_ms": self._first_token_ms,
                "total_ms": int(total * 1000),
                "token_count": self._token_count,
                "promptChars": len(messages[-1]["content"]) if messages else 0,
                "replyChars": sum(len(p) for p in parts),
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "firstTokenMs": self._first_token_ms,
                "mutations": list(self._mutations),
                "outcome": "aborted" if aborted else ("error" if error is not None else "ok"),
            }
            self.diagnosticsChanged.emit()
        if aborted:
            if current:
                if parts:
                    self._history.append({"role": "assistant", "content": "".join(parts)})
                elif self._history and self._history[-1]["role"] == "user":
                    self._history.pop()
            self._deliver_end(gen, paced, self.streamAborted, immediate=True)
            return
        if error is not None:
            if current and self._history and self._history[-1]["role"] == "user":
                self._history.pop()
            self._deliver_end(gen, paced, self.errorOccurred, error, immediate=True)
            return
        if current:
            self._history.append({"role": "assistant", "content": "".join(parts)})
        self._deliver_end(gen, paced, self.streamFinished)

    async def _consume(
        self,
        resp: httpx.Response,
        parts: list[str],
        started: float | None = None,
        generation: int | None = None,
        paced: bool = False,
    ) -> str | None:
        """Relay SSE tokens; returns an error message, or None on a clean finish."""
        gen = self._generation if generation is None else generation
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        # SSE is split on "\n" only (never splitlines), matching the proxy.
        async for raw in resp.aiter_bytes():
            buffer += decoder.decode(raw)
            *lines, buffer = buffer.split("\n")
            for line in lines:
                kind, value = parse_sse_line(line)
                if kind == "token":
                    self._note_first_token(parts, started)
                    parts.append(value)
                    self._relay(gen, paced, value)
                elif kind == "error":
                    return value
                elif kind == "done":
                    return None
        buffer += decoder.decode(b"", final=True)
        kind, value = parse_sse_line(buffer)
        if kind == "token":
            self._note_first_token(parts, started)
            parts.append(value)
            self._relay(gen, paced, value)
        elif kind == "error":
            return value
        return None

    def _note_first_token(self, parts: list[str], started: float | None) -> None:
        self._token_count += 1
        if not parts and started is not None:
            self._first_token_ms = int((time.perf_counter() - started) * 1000)

    def _run_async(
        self,
        coro_factory: Callable[[], Any],
        on_done: Callable[[], None] | None = None,
        cancellable: bool = False,
    ) -> threading.Thread:
        async def runner() -> None:
            with self._lock:
                self._workers[threading.current_thread()] = (
                    asyncio.get_running_loop(),
                    asyncio.current_task(),
                )
            if cancellable:
                with self._lock:
                    self._loop = asyncio.get_running_loop()
                    self._task = asyncio.current_task()
                    if self._abort_requested:
                        self._task.cancel()  # abort arrived before the task existed
            await coro_factory()

        def target() -> None:
            try:
                asyncio.run(runner())
            except (asyncio.CancelledError, httpx.HTTPError, Exception):
                pass  # never let a stream failure or abort reach the Qt runtime
            finally:
                with self._lock:
                    self._workers.pop(threading.current_thread(), None)
                    if cancellable:
                        self._loop = self._task = None
                if on_done:
                    try:
                        on_done()
                    except Exception:  # noqa: BLE001
                        pass

        thread = threading.Thread(target=target, name="strand-ui-io", daemon=True)
        thread.start()
        return thread

    # -- slots ---------------------------------------------------------------------
    @Slot(str, result="QVariantMap")
    def inspectDroppedFile(self, file_url: str) -> dict[str, Any]:
        """Sandbox-check a dropped file and estimate its token cost (ephemeral; nothing persisted)."""
        raw = str(file_url).strip()
        if raw.startswith("file://"):
            raw = urlparse(raw).path
        try:
            path = Path(unquote(raw)).resolve()
        except (OSError, RuntimeError, ValueError):
            return {"safe": False, "error": "invalid_path", "path": raw}
        ok, _ = is_safe_path(path, self._config.resolved_workspace_roots())
        if not ok:
            return {"safe": False, "error": "outside_workspace", "path": str(path)}
        try:
            if not path.is_file():
                return {"safe": False, "error": "not_a_file", "path": str(path)}
            size = path.stat().st_size
        except OSError:
            return {"safe": False, "error": "unreadable", "path": str(path)}
        is_image = path.suffix.lower() in IMAGE_MIME_TYPES
        tokens = IMAGE_TOKEN_ESTIMATE if is_image else max(1, min(size, MAX_BATCH_FILE_BYTES) // 4)
        return {
            "safe": True,
            "path": str(path),
            "name": path.name,
            "tokens": tokens,
            "is_image": is_image,
            "size_bytes": size,
        }

    def _attachment_preamble(self, attachments: Any) -> str:
        """Prompt header naming re-validated attachments so Model A inspects them via tools."""
        lines: list[str] = []
        for item in attachments or []:
            info = self.inspectDroppedFile(str(item))
            if not info.get("safe"):
                continue
            tool = "read_image" if info["is_image"] else "read_file"
            lines.append(f"- {info['path']} (~{info['tokens']} tokens, use {tool})")
        if not lines:
            return ""
        return "[Attached files: inspect them with the workspace tools (read_file, read_image)]\n" + "\n".join(lines) + "\n\n"

    @Slot(str, "QVariantList")
    def sendMessage(self, text: str, attachments: Any = None) -> None:
        text = text.strip()
        if not text:
            return
        prompt = self._attachment_preamble(attachments) + text
        with self._lock:
            if self._busy:
                return
            self._abort_requested = False
            self._set_busy(True)
        self._remember(text)
        self._start_turn(prompt, locked=True)

    def _start_turn(self, text: str, locked: bool = False) -> None:
        """Append `text` as a user turn and stream the reply (no-op while busy)."""
        if not locked:
            with self._lock:
                if self._busy:
                    return
                self._abort_requested = False
                self._set_busy(True)
        self._history.append({"role": "user", "content": text})
        messages = list(self._history)
        generation = self._generation
        self._end_distilling()  # a new turn supersedes the previous weave feedback
        self._begin_strike()
        self._run_async(
            lambda: self._stream(messages, generation, paced=True),
            on_done=self._turn_done,
            cancellable=True,
        )

    @Slot()
    def checkConnection(self) -> None:
        self._run_async(self._check)

    # -- settings (Settings modal) ---------------------------------------------------
    @Slot(result="QVariantMap")
    def getConfig(self) -> dict[str, Any]:
        """Active settings for the Settings modal. The upstream API key is never returned."""
        cfg = load_config()
        file_values = _read_file(default_config_path())
        return {
            "persona_prompt": cfg.persona_prompt,
            "upstream_url": cfg.upstream_url,
            "model_default": cfg.model_default,
            "extractor_url": cfg.extractor_url,
            "extractor_model": cfg.extractor_model,
            "decay_enabled": cfg.decay_enabled,
            "decay_half_life_days": cfg.decay_half_life_days,
            "upstream_api_key_set": bool(cfg.upstream_api_key),
            "redact_secrets": _as_bool(file_values.get("redact_secrets", True)),
            "enable_host_telemetry": cfg.enable_host_telemetry,
            "web_search_enabled": cfg.web_search_enabled,
        }

    @Slot("QVariantMap", result=bool)
    def saveConfig(self, data: dict[str, Any]) -> bool:
        """Merge recognised keys into config.json atomically (dir 0700, file 0600).

        Unknown keys are ignored; an empty `upstream_api_key` keeps the stored key.
        Returns False (file untouched) on invalid input or an I/O error.
        """
        try:
            path = default_config_path()
            current = _read_file(path, strict=True)
            updates: dict[str, Any] = {}
            for key, kind in SETTINGS_KEYS.items():
                if key not in data:
                    continue
                value = data[key]
                if kind == "bool":
                    updates[key] = _as_bool(value)
                elif kind == "float":
                    updates[key] = _validate_half_life(value)
                else:
                    text = "" if value is None else str(value).strip()
                    if kind == "secret" and not text:
                        continue
                    updates[key] = text
            current.update(updates)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            secure_parent_dir(path.parent)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(current, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except (OSError, ValueError, TypeError):
            return False
        return True

    @Slot(str, str, str, str, result=bool)
    def configureEngine(self, provider: str, apiKey: str, model: str, endpointUrl: str = "") -> bool:
        """First-run setup: persist an inference engine ("gemini" or "ollama") to config.json.

        Atomic write (dir 0700, file 0600). Returns False, file untouched, on invalid
        input, an unreachable Ollama or an I/O error.
        """
        try:
            save_config(engine_updates(provider, apiKey, model, endpointUrl))
            self._config = load_config()
        except (OSError, ValueError):
            return False
        self._reload_daemon()
        self.setModel(resolve_model(self._config))
        self.configuredChanged.emit()
        self.diagnosticsChanged.emit()
        return True

    @Slot(str, result=str)
    def folderPath(self, file_url: str) -> str:
        """Local path (home collapsed to `~`) for a FolderDialog `file://` URL; "" if not a local file URL."""
        parsed = urlparse(file_url)
        if parsed.scheme != "file" or not parsed.path:
            return ""
        return _tilde(unquote(parsed.path))

    @Slot("QVariantMap", result="QVariantMap")
    def completeSetup(self, data: dict[str, Any]) -> dict[str, Any]:
        """Finish the first-run wizard.

        Validates everything first, anchors `(Operator) -[operates_host]-> (Host)` in the graph
        (user_explicit, 1.0; no schema change), writes engine, operator name, telemetry flag and
        workspace roots to config.json in one atomic write (0600), writes the systemd user
        drop-in and reloads the service in the background. Returns `{ok, ready, ...}` or
        `{ok: False, step, error}`; on failure config.json is untouched.
        """
        try:
            updates = engine_updates(
                str(data.get("provider") or ""),
                str(data.get("api_key") or ""),
                str(data.get("model") or ""),
                str(data.get("endpoint_url") or ""),
            )
        except ValueError as exc:
            return {"ok": False, "step": 2, "error": str(exc)}
        name = " ".join(str(data.get("operator") or "").split())[:64]
        if any(ord(c) < 32 for c in name):
            return {"ok": False, "step": 3, "error": "The name contains invalid characters."}
        name = anchor_operator_name({"user": name or operator_name()})
        roots: list[str] = []
        for raw in data.get("roots") or []:
            root = str(raw).strip()
            if any(ord(c) < 32 for c in root):
                return {"ok": False, "step": 4, "error": "A folder path contains invalid characters."}
            if root and all(Path(root).expanduser() != Path(r).expanduser() for r in roots):
                roots.append(root)
        if not roots:
            return {"ok": False, "step": 4, "error": "Add at least one folder Strand may read."}
        host = get_host_context()
        hostname = str(host.get("hostname") or "workstation")

        async def _anchor() -> None:
            async with DatabaseManager(self._config.db_path) as db:
                await seed_operator_anchor(
                    db, {"user": name, "hostname": hostname, "os": host.get("os", "")}
                )

        try:
            asyncio.run(_anchor())
            save_config(
                {
                    **updates,
                    "operator_name": name,
                    "enable_host_telemetry": _as_bool(data.get("telemetry", False)),
                    "workspace_roots": roots,
                }
            )
            self._config = load_config()
            _write_systemd_dropin(roots)
        except (OSError, ValueError, RuntimeError) as exc:
            return {"ok": False, "step": 4, "error": f"Could not save setup: {exc}"}
        self._operator_override = name
        self.setModel(resolve_model(self._config))
        self.operatorChanged.emit()
        self.configuredChanged.emit()
        self.diagnosticsChanged.emit()
        if self._client_factory is None:  # injected transport (tests): no real service
            threading.Thread(target=self._reload_service, name="strand-reload", daemon=True).start()
        shown = [_tilde(str(Path(r).expanduser())) for r in roots]
        view = shown[0] if len(shown) == 1 else " and ".join(shown) if len(shown) == 2 else f"{len(shown)} folders"
        ready = f"Systems ready, {name}. Anchored to {hostname} with {view} in view. What are we getting into?"
        return {"ok": True, "ready": ready, "operator": name, "hostname": hostname}

    def _reload_service(self) -> None:
        """Background: pick up the new drop-in (`daemon-reload`), then restart onto the new config."""
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return
        self._reload_daemon()

    def _reload_daemon(self) -> None:
        """Make the running daemon pick up the freshly written config before the first chat.

        The daemon reads config.json once at startup, so a service started before first-run
        setup would answer the first chat turn with 401. Restart the systemd user service (only
        when it is active and the bridge talks to the real transport), then wait for `/health`.
        """
        if self._client_factory is not None:
            return  # injected transport (tests): no real service to restart
        try:
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "strand"], check=False, timeout=10
            ).returncode == 0
            if not active:
                return
            if subprocess.run(["systemctl", "--user", "restart", "strand"], check=False, timeout=60).returncode != 0:
                return
        except (OSError, subprocess.SubprocessError):
            return
        self._uds_path, self._base_url = resolve_endpoint(self._config)
        asyncio.run(self._wait_healthy())

    async def _wait_healthy(self, attempts: int = 40, delay: float = 0.25) -> bool:
        """Poll `GET /health` (fresh client per probe, so no stale connection) until it answers."""
        for _ in range(attempts):
            try:
                async with self.make_client() as client:
                    if (await client.get("/health", timeout=2.0)).status_code == 200:
                        return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(delay)
        return False

    @Slot(str, str)
    @Slot(str, str, str)
    def probeModels(self, endpointUrl: str, apiKey: str, provider: str = "") -> None:
        """List chat models at an endpoint off-thread; failures are silent (`probeStatus == "failed"`)."""
        self._probe_seq += 1
        seq = self._probe_seq
        self._probing_models = True
        self._probe_status = "probing"
        self.isProbingModelsChanged.emit()
        self.probeStatusChanged.emit()
        self._run_async(lambda: self._probe_models(seq, endpointUrl, apiKey, provider))

    async def _probe_models(self, seq: int, endpoint_url: str, api_key: str, provider: str) -> None:
        models: list[str] | None = None
        try:
            base = endpoint_url.strip().rstrip("/")
            if base.startswith(("http://", "https://")):
                ollama = provider.strip().lower() == "ollama"
                target = (base.removesuffix("/v1") + "/api/tags") if ollama else f"{base}/models"
                headers = {"Authorization": f"Bearer {api_key.strip()}"} if api_key.strip() else {}
                async with self._make_model_client() as client:
                    resp = await client.get(target, headers=headers)
                    resp.raise_for_status()
                    models = parse_model_names(resp.json(), ollama)
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            models = None
        self._probeResult.emit(models is not None, [seq] + (models or []))

    def _make_model_client(self) -> httpx.AsyncClient:
        if self._probe_factory is not None:
            return self._probe_factory()
        return httpx.AsyncClient(timeout=MODEL_PROBE_TIMEOUT)

    @Slot(bool, list)
    def _on_probe_result(self, ok: bool, payload: list) -> None:
        if not payload or payload[0] != self._probe_seq:
            return  # superseded by a newer probe
        self._available_models = (
            sorted((str(m) for m in payload[1:]), key=lambda m: _model_sort_key(m, self._model)) if ok else []
        )
        self._probing_models = False
        self._probe_status = "success" if ok else "failed"
        self.availableModelsChanged.emit()
        self.isProbingModelsChanged.emit()
        self.probeStatusChanged.emit()

    @Slot()
    @Slot(str)
    def probeExtractorModels(self, url: str = "http://127.0.0.1:11434/v1") -> None:
        """List local Ollama models off-thread; failure leaves `availableExtractorModels` empty."""
        self._extractor_probe_seq += 1
        seq = self._extractor_probe_seq
        self._extractor_probe_status = "probing"
        self.extractorProbeStatusChanged.emit()
        self._run_async(lambda: self._probe_extractor_models(seq, url))

    async def _probe_extractor_models(self, seq: int, url: str) -> None:
        models: list[str] | None = None
        try:
            base = url.strip().rstrip("/")
            if base.startswith(("http://", "https://")):
                root = base.removesuffix("/v1")
                async with self._make_tags_client() as client:
                    try:
                        resp = await client.get(f"{root}/api/tags")
                        resp.raise_for_status()
                        models = parse_model_names(resp.json(), True)
                    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
                        resp = await client.get(f"{base}/models")
                        resp.raise_for_status()
                        models = parse_model_names(resp.json(), False)
        except Exception:  # noqa: BLE001 - an offline or odd endpoint is a status, never an error
            models = None
        self._extractorModelsResult.emit(seq, models is not None, models or [])

    def _make_tags_client(self) -> httpx.AsyncClient:
        if self._probe_factory is not None:
            return self._probe_factory()
        return httpx.AsyncClient(timeout=EXTRACTOR_TAGS_TIMEOUT)

    @Slot(int, bool, list)
    def _on_extractor_models_result(self, seq: int, ok: bool, models: list) -> None:
        if seq != self._extractor_probe_seq:
            return  # superseded by a newer probe
        self._available_extractor_models = sorted((str(m) for m in models), key=_extractor_sort_key) if ok else []
        self._extractor_probe_status = "success" if ok else "failed"
        self.availableExtractorModelsChanged.emit()
        self.extractorProbeStatusChanged.emit()

    @Slot(str)
    def testExtractor(self, url: str) -> None:
        """Probe the local SLM endpoint off-thread; result arrives as `extractorStatusChanged`."""
        self._run_async(lambda: self._probe_extractor(url))

    async def _probe_extractor(self, url: str) -> None:
        base = url.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            self.extractorStatusChanged.emit(False, "Enter an http(s) URL")
            return
        candidates = [f"{base}/models"]
        if not base.endswith("/v1"):
            candidates.append(f"{base}/v1/models")
        message = "Unreachable"
        try:
            async with self._make_probe_client() as client:
                for target in candidates:
                    resp = await client.get(target)
                    if resp.status_code < 400:
                        self.extractorStatusChanged.emit(True, "Connected")
                        return
                    message = f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            message = "Timed out"
        except httpx.HTTPError as exc:
            message = f"Unreachable: {type(exc).__name__}"
        self.extractorStatusChanged.emit(False, message)

    def _make_probe_client(self) -> httpx.AsyncClient:
        if self._probe_factory is not None:
            return self._probe_factory()
        return httpx.AsyncClient(timeout=EXTRACTOR_TIMEOUT)

    @Slot(result=bool)
    def abort(self) -> bool:
        """Cancel the in-flight stream (if any); partial output is kept. True if one was running."""
        with self._lock:
            if not self._busy:
                return False
            self._abort_requested = True
            if self._loop is not None and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except (RuntimeError, asyncio.CancelledError):
                    pass  # loop already closed: the turn is finishing on its own
            return True

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel every in-flight worker (stream and connection check) and join its thread.

        Called when the window closes so no `strand-ui-io` thread outlives the GUI.
        """
        with self._lock:
            self._abort_requested = True
            workers = list(self._workers.items())
        for thread, (loop, task) in workers:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop already closed: the worker is finishing on its own
        for thread, _ in workers:
            thread.join(timeout)

    @Slot()
    def newConversation(self) -> None:
        """Drop history, abort any running turn and start a fresh session id immediately."""
        self._generation += 1
        if self._busy:
            self.abort()
        self._buffer.clear()
        self._set_grounding(False, "")
        self._history.clear()
        self._turn = {}
        self._session_id = f"ui-{uuid.uuid4().hex[:12]}"
        self.sessionChanged.emit()
        self.diagnosticsChanged.emit()
        self.conversationReset.emit()

    @Slot(str)
    def setModel(self, name: str) -> None:
        name = name.strip()
        if name and name != self._model:
            self._model = name
            self.modelChanged.emit()
            self.diagnosticsChanged.emit()

    @Slot(bool)
    def setPrivateMode(self, enabled: bool) -> None:
        if enabled != self._private:
            self._private = enabled
            self.privateChanged.emit()
            self.diagnosticsChanged.emit()

    @Slot(str, result=str)
    def runCommand(self, text: str) -> str:
        """Execute a client-side slash command; returns its feedback, or "" for a normal prompt.

        Commands never reach the daemon or the upstream model.
        """
        parsed = parse_command(text)
        if parsed is None:
            return ""
        name, arg = parsed
        self._remember(text.strip())
        if name == "new":
            self.newConversation()
            return "Started a new session."
        if name == "private":
            value = arg.lower()
            if value not in ("on", "off"):
                state = "on" if self._private else "off"
                return f"Private mode is {state}. Usage: /private on|off"
            self.setPrivateMode(value == "on")
            return f"Private context {'enabled' if self._private else 'disabled'}."
        if name == "model":
            if not arg:
                return f"Model: {self._model}. Usage: /model <name>"
            self.setModel(arg)
            return f"Model set to {self._model}."
        if name == "help":
            return HELP_TEXT
        return f"Unknown command /{name}. {HELP_TEXT}"

    # -- input history (Up/Down in the input bar) ---------------------------------------
    def _remember(self, text: str) -> None:
        if text and (not self._prompts or self._prompts[-1] != text):
            self._prompts.append(text)
            del self._prompts[:-HISTORY_LIMIT]
        self._prompt_index = None

    @Slot(result=str)
    def historyPrevious(self) -> str:
        if not self._prompts:
            return ""
        if self._prompt_index is None:
            self._prompt_index = len(self._prompts) - 1
        else:
            self._prompt_index = max(0, self._prompt_index - 1)
        return self._prompts[self._prompt_index]

    @Slot(result=str)
    def historyNext(self) -> str:
        if self._prompt_index is None:
            return ""
        self._prompt_index += 1
        if self._prompt_index >= len(self._prompts):
            self._prompt_index = None
            return ""
        return self._prompts[self._prompt_index]

    @Slot(str, result="QVariantList")
    def segments(self, text: str) -> list[dict[str, Any]]:
        return split_markdown(text)

    @Slot(str, result="QVariantList")
    def blocks(self, text: str) -> list[dict[str, Any]]:
        """Presentation blocks (prose bubbles vs. canvas payloads) for an assistant reply."""
        return split_blocks(text)
