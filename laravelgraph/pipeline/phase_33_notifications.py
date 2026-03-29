"""Phase 33 — Notification Channel Enrichment & Mailable Detection.

Enriches existing ``Notification`` nodes with the delivery channels they use
(populated by parsing the ``via()`` method), and creates ``Notification`` nodes
for standalone ``Mailable`` classes so they appear in the graph.

Phase 17 creates ``Notification`` nodes but leaves ``channels`` empty.  This
phase fills that gap, making the graph answer questions like:

  - "What notifications does this feature send?"
  - "Which notifications go via Slack?"
  - "What emails does the order flow send?"

Detection
---------
``via()`` parsing
    Finds classes extending ``Illuminate\\Notifications\\Notification`` (or just
    ``Notification``), extracts the array returned by ``via()`` —
    ``['mail', 'database', 'slack', 'broadcast']`` — and updates the
    ``channels`` field on the existing ``Notification`` node.

``Mailable`` classes
    Finds classes extending ``Mailable`` or ``Illuminate\\Mail\\Mailable``.
    Creates/upserts a ``Notification`` node with ``channels='["mail"]'`` so
    they appear in event/notification queries.

Stats: ``notifications_enriched``, ``mailables_found``
"""

from __future__ import annotations

import json
import re

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────

_EXTENDS_NOTIFICATION_RE = re.compile(
    r'class\s+(\w+)\s+extends\s+(?:[\w\\]*?)?Notification\b',
    re.IGNORECASE,
)
_EXTENDS_MAILABLE_RE = re.compile(
    r'class\s+(\w+)\s+extends\s+(?:[\w\\]*?)?Mailable\b',
    re.IGNORECASE,
)
_NS_RE   = re.compile(r'^\s*namespace\s+([\w\\]+)\s*;', re.MULTILINE)
_VIA_RE  = re.compile(
    r'function\s+via\s*\([^)]*\)\s*(?::\s*\w+\s*)?\{(.*?)\}',
    re.DOTALL,
)
# Matches array items like 'mail', "slack", 'database', etc.
_CHANNEL_ITEM_RE = re.compile(r'["\']([a-z_]+)["\']')


def _fqn_from_content(content: str, class_name: str) -> str:
    ns_match = _NS_RE.search(content)
    ns = ns_match.group(1).replace("\\\\", "\\") if ns_match else ""
    return f"{ns}\\{class_name}" if ns else class_name


def _parse_via_channels(content: str) -> list[str]:
    """Extract channel strings from the ``via()`` method body."""
    m = _VIA_RE.search(content)
    if not m:
        return []
    body = m.group(1)
    return _CHANNEL_ITEM_RE.findall(body)


def run(ctx: PipelineContext) -> None:
    """Enrich Notification channels and detect Mailable classes."""
    enriched  = 0
    mailables = 0

    for php_file in ctx.php_files:
        rel = str(php_file.relative_to(ctx.project_root))
        if "/tests/" in rel or rel.startswith("tests/"):
            continue

        try:
            content = php_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # ── Notification enrichment ─────────────────────────────────────────
        for m in _EXTENDS_NOTIFICATION_RE.finditer(content):
            class_name = m.group(1)
            fqn = _fqn_from_content(content, class_name)
            channels = _parse_via_channels(content)
            if not channels:
                continue

            channels_json = json.dumps(channels)
            nid = make_node_id("notification", fqn)

            # Upsert: create or update the Notification node with channels
            ctx.db.upsert_node("Notification", {
                "node_id":  nid,
                "name":     class_name,
                "fqn":      fqn,
                "file_path": rel,
                "channels": channels_json,
            })
            enriched += 1

        # ── Mailable detection ──────────────────────────────────────────────
        for m in _EXTENDS_MAILABLE_RE.finditer(content):
            class_name = m.group(1)
            fqn = _fqn_from_content(content, class_name)
            nid = make_node_id("notification", fqn)

            ctx.db.upsert_node("Notification", {
                "node_id":  nid,
                "name":     class_name,
                "fqn":      fqn,
                "file_path": rel,
                "channels": '["mail"]',
            })
            mailables += 1

    ctx.stats["notifications_enriched"] = enriched
    ctx.stats["mailables_found"]        = mailables
    logger.info(
        "Phase 33 complete",
        notifications_enriched=enriched,
        mailables_found=mailables,
    )
