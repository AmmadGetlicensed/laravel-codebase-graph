"""Phase 23 — Scheduled Task Parsing.

Parse Console/Kernel.php (Laravel 9/10) or bootstrap/app.php (Laravel 11+)
to extract scheduled task definitions and create ScheduledTask nodes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Match the schedule() method body in Kernel.php or withSchedule() in bootstrap/app.php
_SCHEDULE_METHOD_RE = re.compile(
    r"function\s+schedule\s*\([^)]*\)[^{]*\{(.*?)\n\s*\}",
    re.DOTALL,
)
_SCHEDULE_CLOSURE_RE = re.compile(
    r"->withSchedule\s*\(\s*function\s*\([^)]*\)\s*\{(.*?)\}\s*\)",
    re.DOTALL,
)

# Frequency method regex — matches ->frequency() at end of chain
_FREQUENCY_RE = re.compile(
    r"->(everyMinute|everyTwoMinutes|everyThreeMinutes|everyFourMinutes|"
    r"everyFiveMinutes|everyTenMinutes|everyFifteenMinutes|everyThirtyMinutes|"
    r"everyTwoHours|everyThreeHours|everySixHours|hourly|hourlyAt|"
    r"dailyAt|daily|twiceDaily|twiceDailyAt|weeklyOn|weekly|"
    r"monthlyOn|monthly|quarterly|yearly|"
    r"cron)\s*\(([^)]*)\)"
)

# Modifiers
_WITHOUT_OVERLAPPING_RE = re.compile(r"->withoutOverlapping\s*\(")
_ON_ONE_SERVER_RE = re.compile(r"->onOneServer\s*\(")
_IN_BACKGROUND_RE = re.compile(r"->inBackground\s*\(")
_TIMEZONE_RE = re.compile(r"->timezone\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

# Schedule entry types
_COMMAND_RE = re.compile(r"\$schedule\s*->\s*command\s*\(\s*['\"]([^'\"]+)['\"]\s*")
_JOB_RE = re.compile(r"\$schedule\s*->\s*job\s*\(\s*new\s+([\w\\]+)\s*\(")
_JOB_CLASS_RE = re.compile(r"\$schedule\s*->\s*job\s*\(\s*([\w\\]+)::class\s*")
_CALL_RE = re.compile(r"\$schedule\s*->\s*call\s*\(")
_EXEC_RE = re.compile(r"\$schedule\s*->\s*exec\s*\(\s*['\"]([^'\"]+)['\"]\s*")


def _parse_frequency(statement: str) -> tuple[str, str]:
    """Return (frequency_label, cron_expression) from a schedule statement."""
    m = _FREQUENCY_RE.search(statement)
    if not m:
        return ("custom", "")

    method = m.group(1)
    args = m.group(2).strip().strip("'\"")

    if method == "cron":
        return ("cron", args)
    if method in ("hourlyAt", "dailyAt", "twiceDaily", "twiceDailyAt", "weeklyOn", "monthlyOn"):
        return (method, args)
    return (method, "")


def _parse_modifiers(statement: str) -> dict[str, Any]:
    """Extract boolean modifiers and timezone from a statement."""
    return {
        "without_overlapping": bool(_WITHOUT_OVERLAPPING_RE.search(statement)),
        "on_one_server": bool(_ON_ONE_SERVER_RE.search(statement)),
        "in_background": bool(_IN_BACKGROUND_RE.search(statement)),
        "timezone": (_TIMEZONE_RE.search(statement) or type("", (), {"group": lambda s, n: ""})()).group(1) or "",
    }


def _extract_schedule_body(source: str) -> str:
    """Find the schedule method body in Kernel.php or bootstrap/app.php."""
    m = _SCHEDULE_METHOD_RE.search(source)
    if m:
        return m.group(1)
    m = _SCHEDULE_CLOSURE_RE.search(source)
    if m:
        return m.group(1)
    return ""


def _split_statements(body: str) -> list[str]:
    """Split schedule body into individual $schedule->... chains."""
    # Split on new $schedule-> occurrences
    statements: list[str] = []
    # A statement starts at $schedule-> and ends at ; (accounting for method chains)
    pattern = re.compile(r"\$schedule\s*->.*?;", re.DOTALL)
    for m in pattern.finditer(body):
        statements.append(m.group(0))
    return statements


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run(ctx: PipelineContext) -> None:
    """Parse scheduled task definitions and create ScheduledTask nodes."""
    db = ctx.db
    scheduled_tasks = 0

    # Candidate files for schedule definitions
    candidate_paths: list[Path] = []

    kernel_path = ctx.project_root / "app" / "Console" / "Kernel.php"
    if kernel_path.exists():
        candidate_paths.append(kernel_path)

    bootstrap_app = ctx.project_root / "bootstrap" / "app.php"
    if bootstrap_app.exists():
        candidate_paths.append(bootstrap_app)

    # Also search php_files as fallback
    if not candidate_paths:
        for p in ctx.php_files:
            if p.name in ("Kernel.php", "app.php"):
                candidate_paths.append(p)

    if not candidate_paths:
        logger.info("No schedule definition file found; skipping phase 23")
        ctx.stats["scheduled_tasks"] = 0
        return

    for source_path in candidate_paths:
        source = _read_text(source_path)
        if not source or "$schedule" not in source:
            continue

        body = _extract_schedule_body(source)
        if not body:
            # Try scanning the whole file for $schedule-> patterns
            body = source

        statements = _split_statements(body)
        logger.info("Found schedule statements", path=str(source_path), count=len(statements))

        for stmt in statements:
            frequency, cron_expr = _parse_frequency(stmt)
            modifiers = _parse_modifiers(stmt)

            # Determine entry type and name
            entry_name = ""
            entry_type = "unknown"

            cmd_match = _COMMAND_RE.search(stmt)
            job_match = _JOB_RE.search(stmt)
            job_class_match = _JOB_CLASS_RE.search(stmt)
            exec_match = _EXEC_RE.search(stmt)
            call_match = _CALL_RE.search(stmt)

            if cmd_match:
                entry_name = cmd_match.group(1)
                entry_type = "command"
            elif job_match:
                entry_name = job_match.group(1)
                entry_type = "job"
            elif job_class_match:
                entry_name = job_class_match.group(1)
                entry_type = "job"
            elif exec_match:
                entry_name = exec_match.group(1)
                entry_type = "exec"
            elif call_match:
                entry_name = "closure"
                entry_type = "call"

            if not entry_name:
                continue

            task_key = f"{entry_type}:{entry_name}"
            task_nid = make_node_id("schedule", task_key)

            # Try to determine line number
            line = source.count("\n", 0, source.find(stmt)) + 1 if stmt in source else 0

            try:
                db._insert_node("ScheduledTask", {
                    "node_id": task_nid,
                    "name": entry_name,
                    "command": entry_name if entry_type in ("command", "exec") else "",
                    "frequency": frequency,
                    "cron_expression": cron_expr,
                    "timezone": modifiers["timezone"],
                    "without_overlapping": modifiers["without_overlapping"],
                    "on_one_server": modifiers["on_one_server"],
                    "in_background": modifiers["in_background"],
                    "file_path": str(source_path),
                    "line": line,
                })
                scheduled_tasks += 1
            except Exception as exc:
                logger.debug("ScheduledTask node insert failed", task=task_key, error=str(exc))
                continue

            # SCHEDULES: ScheduledTask → Command or Job (if resolved)
            if entry_type == "command":
                # Try to find a Command node with this signature
                try:
                    rows = db.execute(
                        "MATCH (c:Command) WHERE c.signature STARTS WITH $sig RETURN c.node_id AS nid LIMIT 1",
                        {"sig": entry_name.split(" ")[0]},
                    )
                    if rows:
                        target_nid = rows[0]["nid"]
                        db.upsert_rel(
                            "SCHEDULES",
                            "ScheduledTask",
                            task_nid,
                            "Command",
                            target_nid,
                            {"frequency": frequency},
                        )
                except Exception as exc:
                    logger.debug("SCHEDULES→Command rel failed", task=task_key, error=str(exc))

            elif entry_type == "job":
                short_name = entry_name.split("\\")[-1]
                job_nid = ctx.fqn_index.get(entry_name, make_node_id("job", entry_name))
                try:
                    db.upsert_rel(
                        "SCHEDULES",
                        "ScheduledTask",
                        task_nid,
                        "Job",
                        job_nid,
                        {"frequency": frequency},
                    )
                except Exception as exc:
                    logger.debug("SCHEDULES→Job rel failed", task=task_key, error=str(exc))

    ctx.stats["scheduled_tasks"] = scheduled_tasks
    logger.info("Scheduled task parsing complete", tasks=scheduled_tasks)
