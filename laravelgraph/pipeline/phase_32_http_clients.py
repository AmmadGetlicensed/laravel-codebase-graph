"""Phase 32 — External HTTP Client Call Detection.

Scans PHP source files for outbound HTTP calls made via Laravel's HTTP client
facade (``Http::``) and Guzzle (``$client->request()``, ``new Client``).
Creates ``HttpClientCall`` nodes and ``CALLS_EXTERNAL`` edges from the calling
method/function.

This makes the "outbound API surface" queryable: agents can answer questions
like "what external APIs does this feature call?" and "which services depend on
Stripe's API?".

Patterns detected
-----------------
laravel_http
    ``Http::get(...)``, ``Http::post(...)``, ``Http::withHeaders()->post(...)``,
    ``Http::withToken(...)->patch(...)``, etc.  Covers chained static calls.
guzzle
    ``$client->request('POST', ...)``, ``$this->http->get(...)``.
curl
    ``curl_exec`` / ``curl_setopt`` with CURLOPT_URL — only simple one-liners.

Stats: ``http_client_calls_found``
"""

from __future__ import annotations

import re
from pathlib import Path

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Laravel Http facade: Http::get/post/put/patch/delete/head/options(...)
_HTTP_FACADE_RE = re.compile(
    r'Http::\s*(?:with\w+\s*\([^)]*\)\s*->\s*)*'
    r'(get|post|put|patch|delete|head|options)\s*\(\s*(["\'])(.*?)\2',
    re.IGNORECASE,
)

# Chained Http facade where the verb isn't on the first call:
# Http::withHeaders([...])->post('url', ...)
_HTTP_FACADE_CHAIN_RE = re.compile(
    r'Http::\s*\w+\s*\(.*?\)\s*(?:->\s*\w+\s*\(.*?\)\s*)*->\s*'
    r'(get|post|put|patch|delete|head|options)\s*\(\s*(["\'])(.*?)\2',
    re.IGNORECASE | re.DOTALL,
)

# Guzzle: $client->request('POST', 'https://...')
_GUZZLE_REQUEST_RE = re.compile(
    r'\$\w+\s*->\s*request\s*\(\s*["\']'
    r'(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["\']'
    r'\s*,\s*(["\'])(.*?)\2',
    re.IGNORECASE,
)

# Guzzle shorthand: $client->get('url'), $client->post('url')
_GUZZLE_SHORTHAND_RE = re.compile(
    r'\$\w+\s*->\s*(get|post|put|patch|delete|head)\s*\(\s*(["\'])(.*?)\2',
    re.IGNORECASE,
)

# curl with URL: curl_setopt($ch, CURLOPT_URL, 'https://...')
_CURL_URL_RE = re.compile(
    r'curl_setopt\s*\([^,]+,\s*CURLOPT_URL\s*,\s*(["\'])(.*?)\1',
    re.IGNORECASE,
)

# Detect the currently-enclosing method/function name (heuristic line-scanner)
_METHOD_RE = re.compile(
    r'(?:public|protected|private|static|\s)*function\s+(\w+)\s*\(',
)


def _extract_class_fqn(content: str) -> str:
    """Extract namespace + class name to build a rough FQN for edge linking."""
    ns_match = re.search(r'^\s*namespace\s+([\w\\]+)\s*;', content, re.MULTILINE)
    cls_match = re.search(r'^\s*(?:abstract\s+)?class\s+(\w+)', content, re.MULTILINE)
    ns = ns_match.group(1).replace("\\\\", "\\") if ns_match else ""
    cls = cls_match.group(1) if cls_match else ""
    if ns and cls:
        return f"{ns}\\{cls}"
    return cls or ""


def _find_enclosing_method(lines: list[str], target_lineno: int) -> str:
    """Walk backwards from target_lineno to find the nearest function name."""
    for i in range(target_lineno - 1, -1, -1):
        m = _METHOD_RE.search(lines[i])
        if m:
            return m.group(1)
    return "__file__"


def run(ctx: PipelineContext) -> None:
    """Detect outbound HTTP calls and write HttpClientCall + CALLS_EXTERNAL."""
    from laravelgraph.core.schema import node_id as _nid

    found = 0
    skipped_test = 0

    for php_file in ctx.php_files:
        rel = str(php_file.relative_to(ctx.project_root))
        # Skip test files — they commonly have HTTP mocks that aren't real calls
        if "/tests/" in rel or "/test/" in rel or rel.startswith("tests/"):
            skipped_test += 1
            continue

        try:
            content = php_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if not content:
            continue

        # Only process files that have HTTP call indicators
        if not re.search(r'Http::|->request\s*\(|curl_setopt|curl_exec', content):
            continue

        lines = content.splitlines()
        class_fqn = _extract_class_fqn(content)

        def _emit(verb: str, url: str, client_type: str, lineno: int) -> None:
            nonlocal found
            method_name = _find_enclosing_method(lines, lineno)
            caller_fqn = (
                f"{class_fqn}::{method_name}" if class_fqn else method_name
            )
            verb = verb.upper()
            nid = make_node_id("httpcall", caller_fqn, verb, url[:80])

            ctx.db.upsert_node("HttpClientCall", {
                "node_id":      nid,
                "caller_fqn":   caller_fqn,
                "http_verb":    verb,
                "url_pattern":  url[:500],
                "client_type":  client_type,
                "file_path":    rel,
                "line_number":  lineno,
            })

            # Try to link to the Method node if it's in the fqn_index
            src_node_id = ctx.fqn_index.get(caller_fqn)
            if src_node_id:
                # Determine node label for the method
                if "::" in caller_fqn:
                    ctx.db.upsert_edge("CALLS_EXTERNAL", "Method", "HttpClientCall",
                                       src_node_id, nid,
                                       {"http_verb": verb, "line": lineno})
                else:
                    ctx.db.upsert_edge("CALLS_EXTERNAL", "Function_", "HttpClientCall",
                                       src_node_id, nid,
                                       {"http_verb": verb, "line": lineno})
            found += 1

        for lineno, line in enumerate(lines, start=1):
            # Laravel Http facade (simple)
            for m in _HTTP_FACADE_RE.finditer(line):
                _emit(m.group(1), m.group(3), "laravel_http", lineno)

            # Guzzle $client->request('POST', 'url')
            for m in _GUZZLE_REQUEST_RE.finditer(line):
                _emit(m.group(1), m.group(3), "guzzle", lineno)

            # Guzzle shorthand $client->get('url') — only if URL looks like http(s)
            for m in _GUZZLE_SHORTHAND_RE.finditer(line):
                url = m.group(3)
                if url.startswith(("http://", "https://", "/", "$")):
                    _emit(m.group(1), url, "guzzle", lineno)

            # curl with CURLOPT_URL
            for m in _CURL_URL_RE.finditer(line):
                _emit("GET", m.group(2), "curl", lineno)

    ctx.stats["http_client_calls_found"] = found
    logger.info(
        "Phase 32 complete",
        http_calls=found,
        skipped_test_files=skipped_test,
    )
