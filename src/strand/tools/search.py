"""Sovereign local `web_search` tool: DuckDuckGo HTML scrape with `httpx` + stdlib only.

Ephemeral by construction: results are returned to the proxy tool loop for the active turn and
never persisted. The outbound query is scrubbed with `sanitize_text` first. Every failure
(timeout, rate limit, empty parse) collapses to one generic error dictionary; nothing raises.
"""

from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from ..sanitize import sanitize_text

SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
SEARCH_TIMEOUT = 8.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 10
MAX_SNIPPET_CHARS = 400
UNAVAILABLE = "Web search temporarily unavailable."

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the live web for recent news, vehicle specifications, real-time documentation, or public facts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string.",
                }
            },
            "required": ["query"],
        },
    },
}


class _ResultParser(HTMLParser):
    """Collect `result__a` (title + href) and `result__snippet` anchors in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._field: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._field is not None:
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if "result__a" in classes:
            self.results.append({"title": "", "url": values.get("href") or "", "snippet": ""})
            self._field = "title"
        elif "result__snippet" in classes and self.results:
            self._field = "snippet"
        else:
            return
        self._buf = []

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._field is not None:
            text = " ".join("".join(self._buf).split())
            if not self.results[-1][self._field]:  # first snippet wins
                self.results[-1][self._field] = text
            self._field = None


def _resolve_url(href: str) -> str:
    """Unwrap DuckDuckGo's `/l/?uddg=` redirect into the destination URL ('' when not http(s))."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
        if parts.path.startswith("/l/"):
            target = parse_qs(parts.query).get("uddg", [""])[0]
            href = unquote(target)
        else:  # ads (/y.js) and internal links are not organic results
            return ""
    return href if href.startswith(("http://", "https://")) else ""


def parse_results(html: str, max_results: int) -> list[dict[str, str]]:
    parser = _ResultParser()
    parser.feed(html)
    parser.close()
    out: list[dict[str, str]] = []
    for item in parser.results:
        url = _resolve_url(item["url"])
        if not url or not item["title"]:
            continue
        out.append({"title": item["title"], "url": url, "snippet": item["snippet"][:MAX_SNIPPET_CHARS]})
        if len(out) >= max_results:
            break
    return out


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=SEARCH_TIMEOUT, headers={"User-Agent": USER_AGENT})


def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> list[dict[str, str]] | dict[str, str]:
    """Top organic DuckDuckGo results as `[{title, url, snippet}]`, or `{"error": ...}`."""
    unavailable = {"error": UNAVAILABLE}
    if not isinstance(query, str) or not query.strip():
        return unavailable
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        max_results = DEFAULT_MAX_RESULTS
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    clean = sanitize_text(query.strip())
    try:
        with _make_client() as client:
            response = client.post(SEARCH_URL, data={"q": clean}, headers={"User-Agent": USER_AGENT})
        if response.status_code != 200:  # 202/429 are DuckDuckGo's rate-limit signals
            return unavailable
        results = parse_results(response.text, max_results)
    except Exception:  # noqa: BLE001 - fail closed: timeouts, transport and parse errors
        return unavailable
    return results or unavailable
