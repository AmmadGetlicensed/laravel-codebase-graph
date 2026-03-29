"""LaravelGraph CLI — built with Typer + Rich."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table
from rich.tree import Tree

def _fmt_sec(seconds: float) -> str:
    if seconds >= 60:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"
    return f"{seconds:.1f}s"


app = typer.Typer(
    name="laravelgraph",
    help=(
        "Graph-powered code intelligence for Laravel codebases.\n\n"
        "Indexes your Laravel project into a local knowledge graph, then exposes it\n"
        "via an MCP server so AI agents (Claude Code, Cursor, Windsurf) can understand\n"
        "your routes, models, events, and code relationships.\n\n"
        "[bold]Typical workflow:[/bold]\n"
        "  1. laravelgraph analyze          [dim]# one-time: index your project[/dim]\n"
        "  2. laravelgraph serve            [dim]# start MCP server for your AI agent[/dim]\n"
        "  3. laravelgraph doctor           [dim]# verify everything is working[/dim]\n\n"
        "[bold]Run any command with --help for full options and examples:[/bold]\n"
        "  laravelgraph analyze --help\n"
        "  laravelgraph routes --help\n\n"
        "[dim]Full reference: laravelgraph guide[/dim]"
    ),
    add_completion=True,
    rich_markup_mode="rich",
)
console = Console()

# ── Shared options ────────────────────────────────────────────────────────────

PathArg = typer.Argument(None, help="Path to the Laravel project root (default: current directory)")
ProjectOpt = typer.Option(None, "--project", "-p", help="Path to the Laravel project root")


def _project_root(path: Optional[Path]) -> Path:
    root = (path or Path.cwd()).resolve()
    if not (root / "composer.json").exists():
        console.print(
            f"[yellow]Warning:[/yellow] No composer.json found at {root}. "
            "This may not be a Laravel project.",
        )
    return root


# ── analyze ───────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="1. Setup & Indexing")
def analyze(
    path: Optional[Path] = PathArg,
    full: bool = typer.Option(False, "--full", help="Force full rebuild. Use on first run or after major refactors. Default: incremental update."),
    no_embeddings: bool = typer.Option(False, "--no-embeddings", help="Skip vector embedding generation. Speeds up indexing; disables semantic (vector) search. BM25 and fuzzy search still work."),
    phases: Optional[str] = typer.Option(None, "--phases", help="Re-run specific pipeline phases only (e.g. '14' or '1,2,14'). Phase 14 = route analysis. Skips all other phases for faster targeted updates."),
    warm_cache: bool = typer.Option(False, "--warm-cache", help="Pre-warm the query cache after indexing: caches SELECT results for the most-accessed tables and small lookup tables so the first agent query is instant."),
) -> None:
    """Index a Laravel project — builds or updates the KuzuDB knowledge graph.

    Parses all PHP, Blade, and config files and stores the result as a graph
    of nodes (classes, methods, routes, models, events) and edges (calls,
    relationships, dispatches).

    Use --full on the first run or after major refactors to guarantee a clean
    rebuild. Subsequent runs are incremental by default.

    Use --phases to re-run only specific pipeline phases without a full
    rebuild. Useful for targeted updates (e.g. '--phases 14' to refresh only
    route analysis after adding new routes).

    Use --warm-cache to pre-populate the query result cache after indexing.
    This runs SELECT queries against the live DB for the most-accessed tables
    so the first agent db-query call is served from cache, not a live DB hit.

    Examples:
      laravelgraph analyze
      laravelgraph analyze /path/to/project --full
      laravelgraph analyze --phases 14
      laravelgraph analyze --phases 24,25,26 --warm-cache
    """
    root = _project_root(path)

    selected_phases = None
    if phases:
        try:
            selected_phases = [int(p.strip()) for p in phases.split(",")]
        except ValueError:
            console.print("[red]Error:[/red] --phases must be comma-separated integers")
            raise typer.Exit(1)

    console.print(Panel(
        f"[bold green]LaravelGraph[/bold green] — Indexing [cyan]{root}[/cyan]",
        subtitle="Building knowledge graph...",
    ))

    from laravelgraph.config import Config
    from laravelgraph.logging import configure
    cfg = Config.load(root)
    configure(cfg.log.level, cfg.log.dir)

    phase_times: list[float] = []
    phase_names: list[str] = []
    total_phases_ref: list[int] = [31]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=False,
    ) as progress:
        # Main task: overall phase progress
        task = progress.add_task("Initializing...", total=total_phases_ref[0])
        # Status task: live in-phase status messages (hidden until a phase calls set_status)
        status_task = progress.add_task("", total=None, visible=False)

        def on_phase_start(idx: int, name: str, total: int, description: str) -> None:
            total_phases_ref[0] = total
            progress.update(
                task,
                total=total,
                description=f"[{idx}/{total}] [bold]{name}[/bold]  [dim]{description}[/dim]",
            )
            # Hide / clear previous status line
            progress.update(status_task, description="", visible=False)

        def on_phase_done(idx: int, name: str, elapsed: float) -> None:
            phase_times.append(elapsed)
            phase_names.append(name)
            progress.update(task, completed=idx)
            # Hide status line after phase finishes
            progress.update(status_task, description="", visible=False)

        def on_status(msg: str) -> None:
            progress.update(status_task, description=f"  [dim]↳ {msg}[/dim]", visible=True)

        from laravelgraph.pipeline.orchestrator import Pipeline
        pipeline = Pipeline(root, config=cfg)

        try:
            ctx = pipeline.run(
                full=full,
                skip_embeddings=no_embeddings,
                phases=selected_phases,
                on_phase_start=on_phase_start,
                on_phase_done=on_phase_done,
                on_phase_status=on_status,
            )
        except Exception as e:
            if "lock" in str(e).lower() or "locked" in str(e).lower():
                console.print(
                    "\n[red]Error:[/red] Database is locked — another laravelgraph process may be running.\n"
                    "Stop it and try again."
                )
                raise typer.Exit(1)
            raise

        progress.update(task, description="[green]✓ Complete![/green]", completed=total_phases_ref[0])

    # Print per-phase summary lines (outside progress bar — rendered after it closes)
    for i, (name, elapsed) in enumerate(zip(phase_names, phase_times)):
        idx = i + 1
        console.print(
            f"  [green]✓[/green]  [dim][{idx}/{total_phases_ref[0]}] {name}[/dim]  [dim]{_fmt_sec(elapsed)}[/dim]"
        )

    # Show stats table
    console.print()
    table = Table(title="Index Summary", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Count", justify="right")

    for key, value in sorted(ctx.stats.items()):
        table.add_row(key.replace("_", " ").title(), str(value))

    console.print(table)

    if ctx.errors:
        console.print(f"\n[yellow]⚠️ {len(ctx.errors)} warnings during indexing.[/yellow]")
        for err in ctx.errors[:5]:
            console.print(f"  [dim]{err}[/dim]")
        if len(ctx.errors) > 5:
            console.print(f"  [dim]...and {len(ctx.errors) - 5} more[/dim]")

    # ── Cache warming (optional) ──────────────────────────────────────────────
    if warm_cache and cfg.databases:
        console.print("\n[dim]Warming query cache...[/dim]")
        try:
            from laravelgraph.mcp.warm_queries import warm_query_cache
            warm_totals = warm_query_cache(root, cfg)
            console.print(
                f"[green]✓[/green] Query cache warmed: "
                f"{warm_totals['warmed']} tables cached, "
                f"{warm_totals['skipped']} skipped, "
                f"{warm_totals['errors']} errors"
            )
        except Exception as e:
            console.print(f"[yellow]Cache warming failed:[/yellow] {e}")
    elif warm_cache and not cfg.databases:
        console.print("[dim]--warm-cache: no DB connections configured, skipping[/dim]")

    # ── Plugin auto-generation (post-analyze) ────────────────────────────────
    index_dir = root / ".laravelgraph"
    plugins_dir = index_dir / "plugins"
    try:
        from laravelgraph.plugins.meta import PluginMetaStore
        from laravelgraph.plugins.self_improve import auto_generate_suggested
        from laravelgraph.core.graph import GraphDB
        _meta = PluginMetaStore(index_dir)
        _gdb = GraphDB(index_dir / "graph.kuzu")
        with console.status("[dim]Scanning for plugin opportunities...[/dim]", spinner="dots"):
            _gen_results = auto_generate_suggested(plugins_dir, _meta, root, _gdb, cfg)
        _gdb.close()
        for _pname, _ok, _msg in _gen_results:
            if _ok:
                console.print(f"[green]✓[/green] Plugin auto-generated: [bold]{_pname}[/bold]")
            else:
                console.print(f"[dim]  Plugin {_pname}: {_msg}[/dim]")
        if not _gen_results:
            console.print("[dim]  Plugins up to date — no new recipes detected[/dim]")
    except Exception as _pe:
        console.print(f"[dim]  Plugin scan skipped: {_pe}[/dim]")

    console.print(
        f"\n[green]✓[/green] Project indexed successfully. "
        f"Run [bold]laravelgraph serve[/bold] to start the MCP server."
    )


# ── status ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="1. Setup & Indexing")
def status(path: Optional[Path] = PathArg) -> None:
    """Show index status for the current project.

    Displays last-indexed timestamp, laravelgraph version used to index,
    node and edge counts, and the on-disk size of the KuzuDB graph database.
    Exits with code 1 if the project has not been indexed yet.
    """
    root = _project_root(path)

    from laravelgraph.config import index_dir
    from laravelgraph.core.registry import Registry

    registry = Registry()
    entry = registry.get(root)

    if not entry:
        console.print(f"[yellow]Not indexed:[/yellow] {root}")
        console.print("Run [bold]laravelgraph analyze[/bold] to index this project.")
        raise typer.Exit(1)

    import datetime
    indexed_at = datetime.datetime.fromtimestamp(entry.indexed_at).strftime("%Y-%m-%d %H:%M:%S")

    console.print(Panel(
        f"[bold]{root.name}[/bold]\n"
        f"Path: {entry.path}\n"
        f"Laravel: {entry.laravel_version} | PHP: {entry.php_version}\n"
        f"Indexed: {indexed_at}",
        title="Project Status",
        border_style="green",
    ))

    if entry.stats:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Metric")
        table.add_column("Count", justify="right")
        for k, v in sorted(entry.stats.items()):
            table.add_row(k.replace("_", " ").title(), str(v))
        console.print(table)

    # Check if graph DB exists
    db_path = index_dir(root) / "graph.kuzu"
    if db_path.exists():
        if db_path.is_file():
            size_mb = db_path.stat().st_size / 1024 / 1024
        else:
            size_mb = sum(f.stat().st_size for f in db_path.rglob("*") if f.is_file()) / 1024 / 1024
        console.print(f"\nGraph DB: [cyan]{db_path}[/cyan] ({size_mb:.1f} MB)")


# ── list ──────────────────────────────────────────────────────────────────────

@app.command(name="list", rich_help_panel="1. Setup & Indexing")
def list_repos() -> None:
    """List all projects indexed by laravelgraph on this machine.

    Reads the global registry at ~/.laravelgraph/repos.json and shows each
    project name, Laravel version, last-indexed timestamp, and path.
    """
    from laravelgraph.core.registry import Registry
    import datetime

    registry = Registry()
    repos = registry.all()

    if not repos:
        console.print("No repositories indexed yet.")
        console.print("Run [bold]laravelgraph analyze /path/to/project[/bold] to get started.")
        return

    table = Table(title="Indexed Repositories", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Laravel")
    table.add_column("Indexed At")
    table.add_column("Path", style="dim")

    for repo in repos:
        indexed = datetime.datetime.fromtimestamp(repo.indexed_at).strftime("%Y-%m-%d %H:%M")
        table.add_row(repo.name, repo.laravel_version, indexed, repo.path)

    console.print(table)


# ── clean ─────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="1. Setup & Indexing")
def clean(
    path: Optional[Path] = PathArg,
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt and delete immediately"),
) -> None:
    """Remove the .laravelgraph/ index directory for the current project.

    Deletes the KuzuDB graph database, summary cache, and project config
    stored in <project>/.laravelgraph/. Also removes the entry from the
    global registry (~/.laravelgraph/repos.json).

    The index is re-created from scratch on the next `laravelgraph analyze`.
    Use --force to skip the confirmation prompt.
    """
    root = _project_root(path)

    if not force:
        confirmed = typer.confirm(f"Delete index for {root.name}? This cannot be undone.")
        if not confirmed:
            console.print("Cancelled.")
            return

    import shutil
    from laravelgraph.config import index_dir
    from laravelgraph.core.registry import Registry

    idx_dir = index_dir(root)
    if idx_dir.exists():
        shutil.rmtree(idx_dir)
        console.print(f"[green]✓[/green] Deleted {idx_dir}")

    Registry().unregister(root)
    console.print(f"[green]✓[/green] Removed from registry.")


# ── download ──────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="1. Setup & Indexing")
def download(
    check: bool = typer.Option(False, "--check", help="Only check status, do not download anything."),
    path: Optional[Path] = PathArg,
) -> None:
    """Download required model files and check dependency status.

    Shows which dependencies are already downloaded and which are missing.
    Downloads any missing assets with progress bars.

    Safe to run multiple times — already-downloaded assets are skipped.

    Examples:
      laravelgraph download           # download any missing dependencies
      laravelgraph download --check   # show status only, no downloads
    """
    from laravelgraph.downloads import DEPENDENCIES, check_all

    console.print(Panel(
        "[bold green]LaravelGraph[/bold green] — Dependency Manager",
        border_style="cyan",
    ))

    statuses = check_all()

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Dependency")
    table.add_column("Description")
    table.add_column("Size", justify="right", width=10)
    table.add_column("Status", width=20)

    for dep in DEPENDENCIES:
        available = statuses[dep.key]
        if available:
            status_text = "[green]✓ Ready[/green]"
        else:
            # Distinguish "pip package missing" (tree-sitter-php) from "downloadable"
            if "parsing" in dep.tags:
                status_text = "[red]✗ Missing[/red]"
            else:
                status_text = "[yellow]⬇ Not downloaded[/yellow]"
        table.add_row(dep.name, dep.description, dep.size_hint, status_text)

    console.print(table)

    all_ready = all(statuses.values())

    if all_ready:
        console.print("\n[green]All dependencies are ready.[/green]")
        return

    if check:
        # --check: show status only, no downloads
        missing_count = sum(1 for v in statuses.values() if not v)
        console.print(
            f"\n[yellow]{missing_count} dependency/dependencies not ready.[/yellow] "
            "Run [bold]laravelgraph download[/bold] to fetch them."
        )
        return

    # Download missing dependencies
    from laravelgraph.downloads import DEPENDENCIES as _DEPS

    missing_deps = [dep for dep in _DEPS if not statuses[dep.key]]
    total = len(missing_deps)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        downloaded = 0
        failed = 0

        for i, dep in enumerate(missing_deps, start=1):
            task = progress.add_task(
                f"[{i}/{total}] Downloading {dep.name}...",
                total=None,  # indeterminate
            )
            try:
                dep.download(None)
                downloaded += 1
                progress.update(
                    task,
                    description=f"[{i}/{total}] [green]✓[/green] {dep.name}",
                    completed=1,
                    total=1,
                )
            except Exception as exc:
                failed += 1
                progress.update(
                    task,
                    description=f"[{i}/{total}] [red]✗[/red] {dep.name}",
                    completed=1,
                    total=1,
                )
                console.print(f"  [red]Error:[/red] {exc}")

    ready = downloaded
    console.print(
        f"\n[green]✓ {ready} dependenc{'y' if ready == 1 else 'ies'} ready[/green]"
        + (f", [red]{failed} failed[/red]" if failed else "")
    )
    if failed:
        raise typer.Exit(1)


# ── query ─────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="2. Explore Your Codebase")
def query(
    query_str: str = typer.Argument(..., help="Natural-language or keyword search query (e.g. 'user authentication', 'PostController', 'send welcome email')"),
    path: Optional[Path] = ProjectOpt,
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results to return (default: 20, max recommended: 100)"),
    role: str = typer.Option("", "--role", "-r", help="Filter by Laravel role. Values: controller, model, event, listener, job, middleware, command, provider, request, resource, policy, observer, exception, rule, cast, channel, notification"),
) -> None:
    """Hybrid search across all indexed Laravel symbols.

    Combines BM25 (keyword), vector (semantic), and fuzzy (typo-tolerant)
    search with Reciprocal Rank Fusion (RRF) to rank results. Works even
    without an LLM provider configured — vector search requires fastembed.

    Use --role to narrow results to a specific Laravel role (e.g. 'model',
    'controller'). Use --limit to control how many results are returned.

    Examples:
      laravelgraph query "user authentication"
      laravelgraph query PostController --role controller
      laravelgraph query "send email" --limit 5
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import Config, index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.search.hybrid import HybridSearch

    cfg = Config.load(root)
    db = GraphDB(index_dir(root) / "graph.kuzu")

    try:
        search = HybridSearch(db, cfg.search)
        search.build_index()
        results = search.search(query_str, limit=limit, role_filter=role or None)
    except Exception as e:
        console.print(f"[red]Search error:[/red] {e}")
        raise typer.Exit(1)

    if not results:
        console.print(f"No results for '{query_str}'")
        return

    table = Table(title=f"Results for '{query_str}'", show_header=True, header_style="bold")
    table.add_column("Type", style="cyan", width=15)
    table.add_column("Symbol")
    table.add_column("Role", width=12)
    table.add_column("File", style="dim")
    table.add_column("Score", justify="right", width=8)

    for r in results:
        table.add_row(
            r.label,
            r.fqn or r.name,
            r.laravel_role or "",
            r.file_path.split("/")[-2:] and "/".join(r.file_path.split("/")[-2:]) or "",
            f"{r.score:.3f}",
        )

    console.print(table)


# ── context ───────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="2. Explore Your Codebase")
def context(
    symbol: str = typer.Argument(..., help="Symbol FQN, class name, or node_id to look up (e.g. 'PostController', 'App\\Http\\Controllers\\PostController')"),
    path: Optional[Path] = ProjectOpt,
) -> None:
    """CLI view of a symbol's 360° relationships: callers, callees, routes, events, models.

    Shows everything that calls this symbol (callers), everything this symbol
    calls (callees), any routes that point to it, events it dispatches, and
    models it uses. Each section is omitted when there are no results.

    This is the CLI equivalent of the MCP tool laravelgraph_context, which
    provides a richer AI-readable format including source code and semantic
    summaries. Use the MCP tool for AI agent workflows; use this command for
    quick human inspection at the terminal.

    Examples:
      laravelgraph context PostController
      laravelgraph context "App\\Http\\Controllers\\PostController@store"
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.mcp.server import _resolve_symbol

    db = GraphDB(index_dir(root) / "graph.kuzu")
    node = _resolve_symbol(db, symbol)

    if not node:
        console.print(f"[red]Symbol not found:[/red] {symbol}")
        console.print("Try: [bold]laravelgraph query {symbol}[/bold]")
        raise typer.Exit(1)

    fqn = node.get("fqn", node.get("name", symbol))
    label = node.get("_label", "?")
    node_id = node.get("node_id", "")

    console.print(Panel(
        f"[bold]{fqn}[/bold]\n"
        f"Type: [cyan]{label}[/cyan] | File: {node.get('file_path', '?')}",
        title="Symbol Context",
    ))

    # Callers
    try:
        callers = db.execute(
            "MATCH (caller)-[r:CALLS]->(target) WHERE target.node_id = $id "
            "RETURN caller.fqn AS fqn, r.confidence AS conf LIMIT 10",
            {"id": node_id},
        )
        if callers:
            tree = Tree("[bold]Callers[/bold]")
            for c in callers:
                tree.add(f"[green]{c.get('fqn', '?')}[/green] (conf: {c.get('conf', '?')})")
            console.print(tree)
    except Exception:
        pass

    # Callees
    try:
        callees = db.execute(
            "MATCH (target)-[r:CALLS]->(callee) WHERE target.node_id = $id "
            "RETURN callee.fqn AS fqn LIMIT 10",
            {"id": node_id},
        )
        if callees:
            tree = Tree("[bold]Callees[/bold]")
            for c in callees:
                tree.add(f"[cyan]{c.get('fqn', '?')}[/cyan]")
            console.print(tree)
    except Exception:
        pass

    # Routes pointing to this symbol
    try:
        route_rows = db.execute(
            "MATCH (r:Route)-[:ROUTES_TO]->(target) WHERE target.node_id = $id "
            "RETURN r.http_method AS method, r.uri AS uri LIMIT 5",
            {"id": node_id},
        )
        if route_rows:
            tree = Tree("[bold]Routes[/bold]")
            for r in route_rows:
                method = r.get("method", "?")
                uri = r.get("uri", "?")
                tree.add(f"[green]{method}[/green] {uri}")
            console.print(tree)
    except Exception:
        pass

    # Events dispatched
    try:
        ev_rows = db.execute(
            "MATCH (target)-[:DISPATCHES]->(e) WHERE target.node_id = $id "
            "RETURN e.fqn AS fqn LIMIT 5",
            {"id": node_id},
        )
        if ev_rows:
            tree = Tree("[bold]Events Dispatched[/bold]")
            for e in ev_rows:
                tree.add(f"[blue]{e.get('fqn', '?')}[/blue]")
            console.print(tree)
    except Exception:
        pass

    # Models used
    try:
        model_rows = db.execute(
            "MATCH (target)-[:CALLS]->(m:EloquentModel) WHERE target.node_id = $id "
            "RETURN m.fqn AS fqn LIMIT 5",
            {"id": node_id},
        )
        if model_rows:
            tree = Tree("[bold]Models Used[/bold]")
            for m in model_rows:
                tree.add(f"[yellow]{m.get('fqn', '?')}[/yellow]")
            console.print(tree)
    except Exception:
        pass


# ── impact ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="3. Code Analysis")
def impact(
    symbol: str = typer.Argument(..., help="Symbol to analyze (FQN, class name, or method e.g. 'PostController@store')"),
    path: Optional[Path] = ProjectOpt,
    depth: int = typer.Option(3, "--depth", "-d", help="How many relationship hops to traverse. 1=direct callers only, 2=callers of callers, 3=full transitive blast radius (default)"),
) -> None:
    """Blast radius analysis — all symbols transitively affected by changing this one.

    Traverses the call graph outward from the target symbol to find everything
    that would break or need review if you changed it. Results are grouped by
    depth: depth 1 = direct callers (will break), depth 2 = indirect (may break),
    depth 3+ = transitive (review before releasing).

    Use --depth to control traversal breadth. Depth 1 is fast; depth 3 is the
    default full blast radius. Depth > 4 may be slow on large codebases.

    For the full AI-readable analysis (with source and explanations), use the
    MCP tool laravelgraph_impact from within Claude Code.

    Examples:
      laravelgraph impact PostController
      laravelgraph impact "App\\Http\\Controllers\\PostController@store" --depth 2
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.analysis.impact import ImpactAnalyzer
    from laravelgraph.mcp.server import _resolve_symbol

    db = GraphDB(index_dir(root) / "graph.kuzu")
    node = _resolve_symbol(db, symbol)

    if not node:
        console.print(f"[red]Symbol not found:[/red] {symbol}")
        raise typer.Exit(1)

    analyzer = ImpactAnalyzer(db)
    result = analyzer.analyze(node["node_id"], depth=depth)

    console.print(Panel(
        f"[bold]{node.get('fqn', symbol)}[/bold]\n"
        f"Total affected symbols: [red]{result.total}[/red]",
        title="Impact Analysis",
    ))

    colors = {1: "red", 2: "yellow", 3: "green"}
    labels = {1: "Direct (will break)", 2: "Indirect (may break)", 3: "Transitive (review)"}

    for d in range(1, depth + 1):
        affected = result.by_depth.get(d, [])
        if not affected:
            continue
        color = colors.get(d, "white")
        label = labels.get(d, f"Depth {d}")
        table = Table(title=f"[{color}]{label}[/{color}] ({len(affected)} symbols)")
        table.add_column("Symbol")
        table.add_column("File", style="dim")
        for sym in affected[:15]:
            table.add_row(sym.get("fqn", "?"), sym.get("file_path", "?"))
        if len(affected) > 15:
            table.add_row(f"...and {len(affected) - 15} more", "")
        console.print(table)


# ── dead-code ─────────────────────────────────────────────────────────────────

@app.command(name="dead-code", rich_help_panel="3. Code Analysis")
def dead_code(
    path: Optional[Path] = PathArg,
    role: str = typer.Option("", "--role", "-r", help="Filter results by Laravel role. Values: controller, model, event, listener, job, middleware, command, provider, request"),
) -> None:
    """Dead code report — unreachable symbols with Laravel-aware exemptions.

    Finds methods and classes that have no incoming call edges in the graph,
    meaning nothing in the codebase calls them directly. Uses Laravel-aware
    heuristics to avoid false positives: controllers, event listeners, jobs,
    Artisan commands, and observers are automatically exempted because they
    are called by the Laravel framework at runtime via conventions, not by
    explicit PHP method calls.

    Use --role to narrow results to a specific Laravel role.

    Note: this is a static analysis tool — it cannot detect dynamic calls
    made via $this->dispatch(), event(), or string-based magic invocation.

    Examples:
      laravelgraph dead-code
      laravelgraph dead-code /path/to/project --role model
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")

    try:
        dead = db.execute(
            "MATCH (m:Method {is_dead_code: true}) RETURN m.fqn AS fqn, m.file_path AS file, "
            "m.line_start AS line ORDER BY m.file_path LIMIT 100"
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not dead:
        console.print("[green]✓ No dead code detected![/green]")
        return

    table = Table(title=f"Dead Code ({len(dead)} symbols)", header_style="bold")
    table.add_column("Symbol")
    table.add_column("Line", justify="right", width=6)
    table.add_column("File", style="dim")

    for d in dead:
        table.add_row(d.get("fqn", "?"), str(d.get("line", "?")), d.get("file", "?"))

    console.print(table)


# ── routes ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="2. Explore Your Codebase")
def routes(
    path: Optional[Path] = PathArg,
    method: str = typer.Option("", "--method", "-m", help="Filter by HTTP method. Values: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"),
    uri: str = typer.Option("", "--uri", "-u", help="Filter routes by URI pattern (partial match, e.g. '/api/users')"),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum routes to show (default: 50)"),
) -> None:
    """Route intelligence table — all routes with middleware and controllers.

    Shows a formatted table of all indexed routes including HTTP method, URI,
    controller class and action method, route name, and middleware stack.

    Use --method to filter by HTTP verb (GET, POST, etc.). Use --uri to filter
    routes by a partial URI match. Use --limit to cap the number of results.

    For deep route exploration (full request lifecycle, middleware chain,
    controller source), use the MCP tool laravelgraph_routes from Claude Code.

    Examples:
      laravelgraph routes
      laravelgraph routes --method GET --uri /api
      laravelgraph routes /path/to/project --limit 100
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")

    try:
        all_routes = db.execute(f"MATCH (r:Route) RETURN r.* LIMIT {limit * 3}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    filtered = all_routes
    if method:
        filtered = [r for r in filtered if method.upper() in (r.get("r.http_method", "") or "")]
    if uri:
        filtered = [r for r in filtered if uri in (r.get("r.uri", "") or "")]
    filtered = filtered[:limit]

    if not filtered:
        console.print("No routes found.")
        return

    table = Table(title=f"Routes ({len(filtered)})", header_style="bold cyan")
    table.add_column("Method", width=8)
    table.add_column("URI")
    table.add_column("Controller")
    table.add_column("Name", style="dim")
    table.add_column("Middleware", style="dim")

    method_colors = {
        "GET": "green", "POST": "blue", "PUT": "yellow",
        "PATCH": "yellow", "DELETE": "red",
    }

    for r in filtered:
        http_method = r.get("r.http_method", "?")
        color = method_colors.get(http_method, "white")
        controller = r.get("r.controller_fqn", "Closure")
        if controller:
            controller = controller.split("\\")[-1]
            action = r.get("r.action_method", "")
            if action:
                controller = f"{controller}::{action}"
        mw_raw = r.get("r.middleware_stack", "[]") or "[]"
        try:
            mw = ", ".join(json.loads(mw_raw)[:2])
        except Exception:
            mw = mw_raw[:30]

        table.add_row(
            f"[{color}]{http_method}[/{color}]",
            r.get("r.uri", "?"),
            controller or "?",
            r.get("r.name", "") or "",
            mw,
        )

    console.print(table)


# ── models ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="2. Explore Your Codebase")
def models(
    path: Optional[Path] = PathArg,
    model_name: str = typer.Option("", "--model", "-m", help="Filter by model name (partial match, e.g. 'User' shows User, UserProfile, etc.)"),
) -> None:
    """Eloquent model relationship map — all models and their relationships.

    Displays each Eloquent model along with its hasMany, belongsTo, hasOne,
    belongsToMany, and other relationship methods as a tree. Shows the
    related model and relationship type for each association.

    Use --model to filter to a specific model by name (partial match).

    For richer model exploration including fillable attributes, casts,
    and scopes, use the MCP tool laravelgraph_models from Claude Code.

    Examples:
      laravelgraph models
      laravelgraph models --model User
      laravelgraph models /path/to/project --model Post
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")

    try:
        eloquent_models = db.execute("MATCH (m:EloquentModel) RETURN m.name AS name, m.db_table AS tbl, m.fqn AS fqn LIMIT 100")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not eloquent_models:
        console.print("No Eloquent models found.")
        return

    for m in eloquent_models:
        if model_name and model_name.lower() not in (m.get("name", "")).lower():
            continue

        tree = Tree(f"[bold cyan]{m.get('name', '?')}[/bold cyan] → [dim]{m.get('tbl', '?')}[/dim]")
        try:
            rels = db.execute(
                "MATCH (model:EloquentModel)-[r:HAS_RELATIONSHIP]->(related) WHERE model.fqn = $fqn "
                "RETURN r.relationship_type AS type, r.method_name AS method, related.name AS rel_name",
                {"fqn": m.get("fqn", "")},
            )
            for rel in rels:
                tree.add(f"[green]{rel.get('method')}()[/green] → {rel.get('type')} → [cyan]{rel.get('rel_name')}[/cyan]")
        except Exception:
            pass
        console.print(tree)


# ── events ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="2. Explore Your Codebase")
def events(path: Optional[Path] = PathArg) -> None:
    """Event → listener → job dispatch chain for the entire codebase.

    Shows the full event dispatch graph: each Event class, the Listeners
    registered to handle it, whether each listener is queued, and any Jobs
    that the listener dispatches downstream.

    This gives a complete picture of async/queued side-effects triggered
    by each event in your application.

    For event dispatch analysis with source code and AI explanations, use
    the MCP tool laravelgraph_events from Claude Code.

    Examples:
      laravelgraph events
      laravelgraph events /path/to/project
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")
    try:
        evs = db.execute("MATCH (e:Event) RETURN e.name AS name, e.fqn AS fqn LIMIT 50")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not evs:
        console.print("No events found.")
        return

    for ev in evs:
        tree = Tree(f"[bold blue]Event:[/bold blue] [cyan]{ev.get('name')}[/cyan]")
        try:
            listeners = db.execute(
                "MATCH (l:Listener)-[:LISTENS_TO]->(e:Event) WHERE e.fqn = $fqn "
                "RETURN l.name AS lname, l.fqn AS lfqn, l.is_queued AS queued",
                {"fqn": ev.get("fqn", "")},
            )
            for li in listeners:
                queued = " [queued]" if li.get("queued") else ""
                listener_branch = tree.add(f"[green]Listener:[/green] {li.get('lname')}{queued}")
                jobs = db.execute(
                    "MATCH (l:Listener)-[:DISPATCHES]->(j:Job) WHERE l.fqn = $fqn RETURN j.name AS jname",
                    {"fqn": li.get("lfqn", "")},
                )
                for job in jobs:
                    listener_branch.add(f"[yellow]Job:[/yellow] {job.get('jname')}")
        except Exception:
            pass
        console.print(tree)


# ── bindings ──────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="2. Explore Your Codebase")
def bindings(path: Optional[Path] = PathArg) -> None:
    """Service container binding map — interface-to-implementation mappings.

    Shows all bindings registered in Laravel's service container via
    ServiceProvider classes. Each binding maps an abstract interface (or
    string key) to a concrete implementation class and shows the binding
    type (bind, singleton, instance, scoped) and the provider that
    registered it.

    Service container bindings are how Laravel resolves dependencies via
    constructor injection. Understanding them is essential for tracing
    which concrete class is used behind an interface.

    For bindings with full source and AI explanations, use the MCP tool
    laravelgraph_bindings from Claude Code.

    Examples:
      laravelgraph bindings
      laravelgraph bindings /path/to/project
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")
    try:
        bds = db.execute("MATCH (b:ServiceBinding) RETURN b.abstract AS abs, b.concrete AS conc, b.binding_type AS type, b.provider_fqn AS prov LIMIT 100")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not bds:
        console.print("No service bindings found.")
        return

    table = Table(title="Service Container Bindings", header_style="bold")
    table.add_column("Abstract", style="cyan")
    table.add_column("Concrete", style="green")
    table.add_column("Type", width=12)
    table.add_column("Provider", style="dim")

    for b in bds:
        abs_name = (b.get("abs", "") or "").split("\\")[-1]
        conc_name = (b.get("conc", "") or "").split("\\")[-1]
        prov_name = (b.get("prov", "") or "").split("\\")[-1]
        table.add_row(abs_name, conc_name, b.get("type", "?"), prov_name)

    console.print(table)


# ── schema ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="2. Explore Your Codebase")
def schema(
    path: Optional[Path] = PathArg,
    table_filter: str = typer.Option("", "--table", "-t", help="Filter by table name (partial match, e.g. 'user' shows users, user_profiles, etc.)"),
) -> None:
    """Database schema extracted from Laravel migration files.

    Parses all migration files and displays the resulting table structure:
    table names, column names, types, and nullability. Columns added by
    multiple migrations are merged into one view of the current schema.

    Use --table to filter to tables matching a partial name.

    Note: the schema is derived from static migration analysis. If you have
    manual DB changes not reflected in migrations, they won't appear here.

    For schema exploration with model relationship context, use the MCP tool
    laravelgraph_schema from Claude Code.

    Examples:
      laravelgraph schema
      laravelgraph schema --table user
      laravelgraph schema /path/to/project --table post
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")
    try:
        tables = db.execute("MATCH (t:DatabaseTable) RETURN t.name AS name LIMIT 50")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not tables:
        console.print("No database tables found.")
        return

    for t in tables:
        tname = t.get("name", "?")
        if table_filter and table_filter not in tname:
            continue

        console.print(f"\n[bold cyan]{tname}[/bold cyan]")
        try:
            cols = db.execute(
                "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) WHERE t.name = $n "
                "RETURN c.name AS col, c.type AS type, c.nullable AS nullable",
                {"n": tname},
            )
            col_table = Table(show_header=True, header_style="dim")
            col_table.add_column("Column")
            col_table.add_column("Type")
            col_table.add_column("Nullable", width=10)
            for col in cols:
                nullable = "✓" if col.get("nullable") else ""
                col_table.add_row(col.get("col", "?"), col.get("type", "?"), nullable)
            console.print(col_table)
        except Exception:
            pass


# ── cypher ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="3. Code Analysis")
def cypher(
    query_str: str = typer.Argument(..., help="Read-only Cypher query. MATCH/RETURN patterns only — CREATE/DELETE/SET are blocked. Use LIMIT to avoid large result sets."),
    path: Optional[Path] = ProjectOpt,
) -> None:
    """Execute a read-only Cypher query against the KuzuDB knowledge graph.

    Runs an arbitrary MATCH/RETURN Cypher query against the graph database.
    Mutation keywords (CREATE, MERGE, DELETE, SET, REMOVE, DROP) are blocked
    for safety. Results are displayed as a Rich table (first 50 rows shown).

    Node labels include: Class_, Method, Route, EloquentModel, Event, Listener,
    Job, ServiceBinding, DatabaseTable, DatabaseColumn, and more.

    Edge types include: CALLS, ROUTES_TO, DISPATCHES, LISTENS_TO, HAS_RELATIONSHIP,
    USES_MODEL, HAS_COLUMN, and more.

    Examples:
      laravelgraph cypher "MATCH (n:Route) RETURN n.uri, n.controller_fqn LIMIT 10"
      laravelgraph cypher "MATCH (c:Class_)-[:CALLS]->(m:Method) RETURN c.name, m.name LIMIT 20"
    """
    root = _project_root(path)
    _ensure_indexed(root)

    # Security check
    forbidden = ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "DROP"]
    for kw in forbidden:
        if kw in query_str.upper():
            console.print(f"[red]Error:[/red] Mutation keyword '{kw}' not allowed.")
            raise typer.Exit(1)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")
    try:
        results = db.execute(query_str)
    except Exception as e:
        console.print(f"[red]Query error:[/red] {e}")
        raise typer.Exit(1)

    if not results:
        console.print("No results.")
        return

    headers = list(results[0].keys())
    table = Table(show_header=True, header_style="bold")
    for h in headers:
        table.add_column(h)

    for row in results[:50]:
        table.add_row(*[str(row.get(h, ""))[:60] for h in headers])

    if len(results) > 50:
        console.print(f"[dim]Showing first 50 of {len(results)} results[/dim]")

    console.print(table)


# ── serve ─────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="4. Server & Agent Integration")
def serve(
    path: Optional[Path] = PathArg,
    watch: bool = typer.Option(False, "--watch", "-w", help="Enable live file watching"),
    http: bool = typer.Option(False, "--http", help="Use HTTP/SSE transport instead of stdio"),
    port: int = typer.Option(None, "--port", help="HTTP port (when --http). Default: 3000 or mcp.port from config"),
    host: str = typer.Option(None, "--host", help="HTTP host (when --http). Default: 127.0.0.1 or mcp.host from config"),
    api_key: str = typer.Option(None, "--api-key", help="Bearer token for HTTP auth (when --http). Also via LARAVELGRAPH_API_KEY env var"),
) -> None:
    """Start the MCP server for AI agent integration."""
    root = _project_root(path)

    from laravelgraph.config import Config
    from laravelgraph.logging import configure

    cfg = Config.load(root)
    configure(cfg.log.level, cfg.log.dir)

    # CLI flags override config; config overrides defaults
    _host = host or cfg.mcp.host or "127.0.0.1"
    _port = port or cfg.mcp.port or 3000
    _api_key = api_key or cfg.mcp.api_key or ""

    if not http:
        # stdio transport — start silently (output breaks MCP protocol)
        from laravelgraph.mcp.server import run_stdio
        run_stdio(root, cfg)
    else:
        import json as _json
        console.print(Panel(
            f"[bold green]LaravelGraph MCP Server — HTTP/SSE[/bold green]",
            border_style="green",
        ))
        console.print(f"  [bold]Project:[/bold]   {root}")
        console.print(f"  [bold]Listening:[/bold]  http://{_host}:{_port}")
        console.print(f"  [bold]SSE endpoint:[/bold]  http://{_host}:{_port}/sse")
        console.print(f"  [bold]Health check:[/bold] http://{_host}:{_port}/health")
        if _api_key:
            console.print(f"  [bold]Auth:[/bold]      Bearer token required")
        else:
            console.print(f"  [yellow]  Auth:[/yellow]      No API key set — open access (use --api-key for production)")

        # Show the agent config snippet
        console.print()
        console.print("[bold]Add to your agent MCP config:[/bold]")
        if _api_key:
            agent_cfg = {
                "laravelgraph": {
                    "type": "sse",
                    "url": f"http://{_host}:{_port}/sse",
                    "headers": {"Authorization": f"Bearer {_api_key}"},
                }
            }
        else:
            agent_cfg = {
                "laravelgraph": {
                    "type": "sse",
                    "url": f"http://{_host}:{_port}/sse",
                }
            }
        console.print(_json.dumps(agent_cfg, indent=2))
        console.print()
        console.print("Press [bold]Ctrl+C[/bold] to stop.\n")

        if watch:
            import threading
            from laravelgraph.watch.watcher import start_watch
            watcher_thread = threading.Thread(
                target=start_watch, args=(root, cfg), daemon=True
            )
            watcher_thread.start()

        from laravelgraph.mcp.server import run_http
        run_http(root, host=_host, port=_port, config=cfg, api_key=_api_key)


# ── watch ─────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="4. Server & Agent Integration")
def watch(path: Optional[Path] = PathArg) -> None:
    """Watch mode — live re-indexing on file changes (no MCP server).

    Monitors the project directory for PHP, Blade, and config file changes
    and automatically re-runs the analysis pipeline when files are modified.

    Unlike `laravelgraph serve --watch` (which starts the MCP server AND
    watches for changes), this command only does re-indexing. Use it when
    you want the graph kept up to date in the background without running
    an MCP server — for example, during active development with a separate
    MCP server already running.

    Requires watchfiles: pip install watchfiles

    Examples:
      laravelgraph watch
      laravelgraph watch /path/to/project
    """
    root = _project_root(path)
    _ensure_indexed(root)

    console.print(f"[bold]Watching[/bold] {root} for changes...")
    console.print("Press Ctrl+C to stop.\n")

    from laravelgraph.config import Config
    from laravelgraph.watch.watcher import start_watch

    cfg = Config.load(root)
    start_watch(root, cfg, interactive=True)


# ── diff ──────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="3. Code Analysis")
def diff(
    base: str = typer.Argument("HEAD", help="Base git revision to compare from. Examples: 'HEAD', 'main', 'HEAD~3', 'abc1234'"),
    head: str = typer.Argument("HEAD", help="Head git revision (default: working tree / current branch tip)"),
    path: Optional[Path] = ProjectOpt,
) -> None:
    """Structural branch comparison — files changed between two git revisions.

    Compares two git revisions and shows which files were added, modified,
    or deleted. Useful for understanding the scope of a branch or PR before
    running impact analysis.

    Pass a single revision (e.g. 'main') to compare that revision against
    HEAD. Pass two revisions to compare them directly (e.g. 'main' 'HEAD').

    For a deeper analysis that maps changed files to affected graph symbols,
    use the MCP tool laravelgraph_detect_changes from Claude Code.

    Examples:
      laravelgraph diff                        # HEAD~1..HEAD
      laravelgraph diff main                   # main..HEAD
      laravelgraph diff main HEAD~3            # main..HEAD~3
      laravelgraph diff HEAD~3 HEAD            # last 3 commits
    """
    root = _project_root(path)
    _ensure_indexed(root)

    # If both args are "HEAD" (defaults), use HEAD~1..HEAD
    if base == "HEAD" and head == "HEAD":
        _base = "HEAD~1"
        _head = "HEAD"
    else:
        _base = base
        _head = head

    console.print(f"[bold]Branch diff:[/bold] {_base}..{_head}")

    try:
        from git import Repo
        repo = Repo(str(root))

        diff_obj = repo.commit(_base).diff(repo.commit(_head))
        changed = [(d.a_path, d.change_type) for d in diff_obj]

        if not changed:
            console.print("No changed files.")
            return

        table = Table(title=f"Changed files: {_base}..{_head}", header_style="bold")
        table.add_column("Change", width=8)
        table.add_column("File")

        for file_path, change_type in changed:
            color = {"A": "green", "D": "red", "M": "yellow"}.get(change_type, "white")
            table.add_row(f"[{color}]{change_type}[/{color}]", file_path)

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# ── providers (internal helpers) ──────────────────────────────────────────────

def _providers_list(path: Optional[Path]) -> None:
    """Display the LLM provider status tables (shared by list sub-command and callback)."""
    root = _project_root(path)

    from laravelgraph.config import Config
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY, provider_status

    cfg = Config.load(root)
    status = provider_status(cfg.llm)
    active = status["active_provider"]

    active_label = f"{PROVIDER_REGISTRY[active]['label']}" if active else ""
    active_str = f"[green]{active}[/green] — {active_label}" if active else "[yellow]none[/yellow] — summaries skipped"
    console.print(Panel(
        f"[bold]LLM Summaries:[/bold] {'[green]enabled[/green]' if status['enabled'] else '[red]disabled[/red]'}\n"
        f"[bold]Active provider:[/bold] {active_str}",
        title="LLM Provider Status",
        border_style="cyan",
    ))

    # ── Cloud providers ───────────────────────────────────────────────────────
    cloud_table = Table(title="Cloud Providers", show_header=True, header_style="bold")
    cloud_table.add_column("Provider", width=14)
    cloud_table.add_column("Label")
    cloud_table.add_column("Status", width=12)
    cloud_table.add_column("Model")
    cloud_table.add_column("Env Var", style="dim")

    for name, info in status["providers"].items():
        if info["local"]:
            continue
        configured = info["configured"]
        is_active = name == active
        status_text = "[green]● active[/green]" if is_active else ("[cyan]● ready[/cyan]" if configured else "[dim]○ not set[/dim]")
        model_col = info["model"] if configured else "[dim]—[/dim]"
        env_col = info["env_var"] or "—"
        cloud_table.add_row(name, PROVIDER_REGISTRY[name]["label"], status_text, model_col, env_col)

    console.print(cloud_table)

    # ── Local providers ───────────────────────────────────────────────────────
    local_table = Table(title="Local Providers", show_header=True, header_style="bold")
    local_table.add_column("Provider", width=14)
    local_table.add_column("Label")
    local_table.add_column("Status", width=12)
    local_table.add_column("Model")
    local_table.add_column("Base URL", style="dim")

    for name, info in status["providers"].items():
        if not info["local"]:
            continue
        configured = info["configured"]
        is_active = name == active
        status_text = "[green]● active[/green]" if is_active else ("[cyan]● ready[/cyan]" if configured else "[dim]○ not set[/dim]")
        model_col = info["model"] if configured else "[dim]—[/dim]"
        url_col = info["base_url"] if configured else "[dim]—[/dim]"
        local_table.add_row(name, PROVIDER_REGISTRY[name]["label"], status_text, model_col, url_col)

    console.print(local_table)

    if not active and status["enabled"]:
        console.print(
            "\n[yellow]No provider configured.[/yellow] "
            "Run [bold]laravelgraph providers add <name>[/bold] to set one up."
        )


def _providers_write_config(cfg_path: "Path", llm_patch: dict) -> None:
    """Deep-merge *llm_patch* into the 'llm' section of *cfg_path* and save."""
    import json as _json
    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = _json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Migrate old "summary" key if present
    if "summary" in existing and "llm" not in existing:
        existing["llm"] = existing.pop("summary")
    s = existing.setdefault("llm", {})
    for k, v in llm_patch.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)   # merge dicts (api_keys, models, base_urls)
        else:
            s[k] = v
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_json.dumps(existing, indent=2), encoding="utf-8")


def _pick_model(info: dict, current_model: str = "", custom_prompt: str = "") -> str:
    """Show numbered model list for a provider; let user pick or type custom.

    *current_model* is pre-selected (press Enter to keep it).
    """
    model_list: list[tuple[str, str]] = info.get("models", [])
    default_model: str = current_model or info.get("default_model", "")

    if not model_list:
        return typer.prompt(custom_prompt or "Model", default=default_model)

    console.print("\n[bold]Available models:[/bold]\n")
    for i, (mid, desc) in enumerate(model_list, start=1):
        current_marker = " [green](current)[/green]" if mid == current_model else (
            " [dim](default)[/dim]" if mid == info.get("default_model", "") else ""
        )
        console.print(f"  [cyan]{i:2}[/cyan]  [bold]{mid}[/bold]{current_marker}")
        console.print(f"       [dim]{desc}[/dim]")
    console.print(f"\n  [cyan] 0[/cyan]  Enter custom model ID\n")

    default_idx = next(
        (str(i) for i, (mid, _) in enumerate(model_list, 1) if mid == default_model),
        "1",
    )
    raw = typer.prompt("Select model number (or type a model ID directly)", default=default_idx)

    if raw.isdigit():
        idx = int(raw)
        if idx == 0:
            return typer.prompt("Model ID")
        if 1 <= idx <= len(model_list):
            chosen_id = model_list[idx - 1][0]
            console.print(f"  → [green]{chosen_id}[/green]")
            return chosen_id
        console.print(f"[yellow]Invalid choice {idx}, using default[/yellow]")
        return default_model or model_list[0][0]
    return raw.strip()


def _prompt_scope(root: "Path", global_flag: bool) -> "Path":
    """Ask user where to save (project vs global) if --global not specified."""
    from laravelgraph.config import global_dir, index_dir as _index_dir
    if not global_flag:
        console.print("\n[bold]Where to save?[/bold]\n")
        console.print(f"  [cyan]1[/cyan]  This project only  ({root / '.laravelgraph' / 'config.json'})")
        console.print(f"  [cyan]2[/cyan]  Global — all projects  (~/.laravelgraph/config.json)\n")
        global_flag = typer.prompt("Save to", default="2") == "2"
    return (global_dir() if global_flag else _index_dir(root)) / "config.json"


# ── providers sub-command group ───────────────────────────────────────────────

providers_app = typer.Typer(
    name="providers",
    help="Manage LLM providers for semantic summary generation.",
    rich_markup_mode="rich",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(providers_app, name="providers", rich_help_panel="4. Server & Agent Integration")


@providers_app.callback()
def _providers_callback(ctx: typer.Context) -> None:
    """Show and manage LLM providers for semantic summary generation.

    Run without a sub-command to list all providers and their status.
    """
    if ctx.invoked_subcommand is None:
        _providers_list(None)


@providers_app.command("list")
def providers_list_cmd(path: Optional[Path] = PathArg) -> None:
    """List all configured LLM providers and their status."""
    _providers_list(path)


@providers_app.command("add")
def providers_add(
    name: str = typer.Argument(..., help="Provider name (e.g. groq, openai, ollama, anthropic)"),
    path: Optional[Path] = PathArg,
    global_: bool = typer.Option(False, "--global", "-g", help="Save to global ~/.laravelgraph/config.json"),
) -> None:
    """Add or reconfigure an LLM provider.

    If the provider is already configured, existing values are pre-filled so
    you can press Enter to keep them and only change what you need.

    Examples:
      laravelgraph providers add groq
      laravelgraph providers add ollama --global
      laravelgraph providers add openai -g
    """
    from laravelgraph.config import Config
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY

    name = name.strip().lower()
    if name not in PROVIDER_REGISTRY:
        known = ", ".join(PROVIDER_REGISTRY.keys())
        console.print(f"[red]Unknown provider:[/red] {name}\nKnown providers: {known}")
        raise typer.Exit(1)

    root = _project_root(path)
    cfg = Config.load(root)
    provider_info = PROVIDER_REGISTRY[name]

    # Load existing values for pre-fill
    existing_key = cfg.llm.api_keys.get(name, "")
    existing_model = cfg.llm.models.get(name, "")
    existing_url = cfg.llm.base_urls.get(name, "")
    is_reconfigure = bool(existing_key or existing_model or existing_url)

    console.print(Panel(
        f"Configuring [bold]{provider_info['label']}[/bold]"
        + (" [dim](updating existing)[/dim]" if is_reconfigure else ""),
        border_style="cyan",
    ))

    llm_patch: dict = {}

    if provider_info.get("local"):
        default_url = existing_url or provider_info["base_url"].replace("/v1", "")
        if existing_url:
            console.print(f"  [dim]Current base URL: {existing_url}[/dim]")
        base_url = typer.prompt("Base URL", default=default_url)
        model = _pick_model(provider_info, current_model=existing_model,
                            custom_prompt="Model name (must be already pulled/loaded locally)")
        llm_patch = {
            "provider": name,
            "base_urls": {name: base_url},
            "models": {name: model},
        }
    else:
        env_var = provider_info.get("env_var", "")
        if env_var:
            console.print(f"  Environment variable: [cyan]{env_var}[/cyan]")
        if existing_key:
            masked = existing_key[:5] + "****"
            console.print(f"  [dim]Current key: {masked}  (press Enter to keep)[/dim]")
        new_key = typer.prompt("API key", hide_input=True, default="")
        final_key = new_key if new_key else existing_key
        if not final_key:
            console.print("[red]API key is required.[/red]")
            raise typer.Exit(1)
        model = _pick_model(provider_info, current_model=existing_model)
        llm_patch = {
            "provider": name,
            "api_keys": {name: final_key},
            "models": {name: model},
        }

    cfg_path = _prompt_scope(root, global_)
    _providers_write_config(cfg_path, llm_patch)

    scope_label = "global" if cfg_path.parent == cfg_path.parent else "project"
    from laravelgraph.config import global_dir
    scope_label = "global" if cfg_path.parent == global_dir() else "project"
    console.print(f"\n[green]✓[/green] Config saved to [cyan]{cfg_path}[/cyan] ({scope_label})")
    console.print(
        f"[bold]Active provider:[/bold] [green]{name}[/green]\n"
        "Run [bold]laravelgraph providers[/bold] to verify — "
        "[bold]laravelgraph providers test[/bold] to run a live check."
    )
    if not provider_info.get("local") and provider_info.get("env_var"):
        console.print(
            f"\n[yellow]Tip:[/yellow] Use an env var instead of storing the key in config:\n"
            f"  [dim]export {provider_info['env_var']}=your-key[/dim]"
        )


@providers_app.command("edit")
def providers_edit(
    name: str = typer.Argument(..., help="Provider name to edit"),
    path: Optional[Path] = PathArg,
    global_: bool = typer.Option(False, "--global", "-g", help="Edit global config"),
    model: str = typer.Option("", "--model", "-m", help="Set model directly (non-interactive)"),
    api_key: str = typer.Option("", "--api-key", help="Set API key directly (non-interactive)"),
    base_url: str = typer.Option("", "--base-url", help="Set base URL directly (non-interactive, local providers only)"),
) -> None:
    """Edit a configured provider's settings.

    Without flags: interactive wizard with existing values pre-filled.
    With flags: update specific fields non-interactively.

    Examples:
      laravelgraph providers edit groq --model llama-3.1-8b-instant
      laravelgraph providers edit ollama --base-url http://localhost:11434
      laravelgraph providers edit openai   # interactive, pre-filled
    """
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY

    name = name.strip().lower()
    if name not in PROVIDER_REGISTRY:
        known = ", ".join(PROVIDER_REGISTRY.keys())
        console.print(f"[red]Unknown provider:[/red] {name}\nKnown providers: {known}")
        raise typer.Exit(1)

    # Non-interactive path: flags provided
    if model or api_key or base_url:
        root = _project_root(path)
        llm_patch: dict = {"models": {}, "api_keys": {}, "base_urls": {}}
        if model:
            llm_patch["models"][name] = model
        if api_key:
            llm_patch["api_keys"][name] = api_key
        if base_url:
            llm_patch["base_urls"][name] = base_url
        # Remove empty dicts
        llm_patch = {k: v for k, v in llm_patch.items() if v}
        cfg_path = _prompt_scope(root, global_)
        _providers_write_config(cfg_path, llm_patch)
        console.print(f"[green]✓[/green] {name} updated.")
        return

    # Interactive path: delegate to add (which pre-fills existing values)
    providers_add(name=name, path=path, global_=global_)


@providers_app.command("remove")
def providers_remove(
    name: str = typer.Argument(..., help="Provider name to remove"),
    path: Optional[Path] = PathArg,
    global_: bool = typer.Option(False, "--global", "-g", help="Remove from global config"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Remove a provider's credentials from config.

    The provider's api_key, model, and base_url are removed. If it was the
    active provider, the active provider is reset to 'auto'.

    Examples:
      laravelgraph providers remove groq
      laravelgraph providers remove groq --force
      laravelgraph providers remove openai --global
    """
    import json as _json
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY

    name = name.strip().lower()
    if name not in PROVIDER_REGISTRY:
        known = ", ".join(PROVIDER_REGISTRY.keys())
        console.print(f"[red]Unknown provider:[/red] {name}\nKnown providers: {known}")
        raise typer.Exit(1)

    root = _project_root(path)
    from laravelgraph.config import global_dir, index_dir as _index_dir
    cfg_path = (global_dir() if global_ else _index_dir(root)) / "config.json"

    if not cfg_path.exists():
        console.print(f"[yellow]No config file found at {cfg_path}[/yellow]")
        raise typer.Exit(1)

    try:
        data = _json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Could not read config:[/red] {exc}")
        raise typer.Exit(1)

    # Migrate old key if needed
    if "summary" in data and "llm" not in data:
        data["llm"] = data.pop("summary")

    llm = data.get("llm", {})
    has_key = name in llm.get("api_keys", {})
    has_model = name in llm.get("models", {})
    has_url = name in llm.get("base_urls", {})

    if not (has_key or has_model or has_url):
        console.print(f"[yellow]{name}[/yellow] is not configured in {cfg_path}")
        raise typer.Exit(1)

    if not force:
        confirmed = typer.confirm(f"Remove {name} from {cfg_path}?")
        if not confirmed:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    if has_key:
        del llm["api_keys"][name]
    if has_model:
        del llm["models"][name]
    if has_url:
        del llm["base_urls"][name]
    # Reset active provider if it was this one
    if llm.get("provider") == name:
        llm["provider"] = "auto"
        console.print(f"[dim]Active provider reset to 'auto'[/dim]")

    cfg_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
    console.print(f"[green]✓[/green] {name} removed from config.")


@providers_app.command("activate")
def providers_activate(
    name: str = typer.Argument(..., help="Provider name to activate (use 'auto' to auto-detect)"),
    path: Optional[Path] = PathArg,
    global_: bool = typer.Option(False, "--global", "-g", help="Save to global config"),
) -> None:
    """Switch the active LLM provider without re-entering credentials.

    The provider must already be configured (have an API key or be a local
    provider with a base URL set). Use 'auto' to let LaravelGraph pick the
    first available cloud provider.

    Examples:
      laravelgraph providers activate groq
      laravelgraph providers activate openai --global
      laravelgraph providers activate auto
    """
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY

    name = name.strip().lower()
    if name not in PROVIDER_REGISTRY and name != "auto":
        known = ", ".join(PROVIDER_REGISTRY.keys())
        console.print(f"[red]Unknown provider:[/red] {name}\nKnown providers: {known}")
        raise typer.Exit(1)

    root = _project_root(path)
    cfg_path = _prompt_scope(root, global_)
    _providers_write_config(cfg_path, {"provider": name})

    from laravelgraph.config import global_dir
    scope_label = "global" if cfg_path.parent == global_dir() else "project"
    console.print(f"[green]✓[/green] Active provider set to [green]{name}[/green] ({scope_label})")
    console.print("Run [bold]laravelgraph providers[/bold] to verify.")


@providers_app.command("test")
def providers_test(
    name: str = typer.Argument("", help="Provider to test (default: currently active provider)"),
    path: Optional[Path] = PathArg,
) -> None:
    """Run a live test prompt against a provider.

    Tests connectivity, authentication, and response quality. If no provider
    name is given, tests the currently active provider.

    Examples:
      laravelgraph providers test
      laravelgraph providers test groq
      laravelgraph providers test openai
    """
    import time as _time
    from laravelgraph.config import Config
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY, provider_status, generate_summary

    root = _project_root(path)
    cfg = Config.load(root)
    status = provider_status(cfg.llm)

    target = name.strip().lower() if name.strip() else status.get("active_provider", "")
    if not target:
        console.print("[yellow]No provider configured.[/yellow] Run [bold]laravelgraph providers add <name>[/bold] first.")
        raise typer.Exit(1)

    if target not in PROVIDER_REGISTRY:
        known = ", ".join(PROVIDER_REGISTRY.keys())
        console.print(f"[red]Unknown provider:[/red] {target}\nKnown providers: {known}")
        raise typer.Exit(1)

    # Build an LLMConfig targeting this specific provider
    from laravelgraph.config import LLMConfig
    test_cfg = LLMConfig(
        enabled=True,
        provider=target,
        api_keys=cfg.llm.api_keys,
        models=cfg.llm.models,
        base_urls=cfg.llm.base_urls,
    )

    pinfo = PROVIDER_REGISTRY[target]
    model = cfg.llm.models.get(target, "") or pinfo["default_model"]
    console.print(f"\nTesting [bold]{pinfo['label']}[/bold] (model: [cyan]{model}[/cyan])...\n")

    t0 = _time.perf_counter()
    summary, used_provider = generate_summary(
        fqn="App\\Http\\Controllers\\HealthController::check",
        node_type="controller method",
        source="public function check(): JsonResponse { return response()->json(['status' => 'ok']); }",
        summary_cfg=test_cfg,
    )
    elapsed = _time.perf_counter() - t0

    if summary:
        console.print(f"[green]✓[/green] Live test passed [dim]({elapsed:.2f}s)[/dim]")
        console.print(f"\n  [dim italic]\"{summary}\"[/dim italic]\n")
    else:
        console.print(f"[red]✗[/red] Live test failed — no response from {target}")
        console.print("[dim]Check your model name, API key, and network connection.[/dim]")
        console.print(f"[dim]To reconfigure: laravelgraph providers add {target}[/dim]")
        raise typer.Exit(1)


# ── configure (hidden backward-compat alias) ──────────────────────────────────

@app.command(rich_help_panel="4. Server & Agent Integration", hidden=True)
def configure(
    path: Optional[Path] = PathArg,
    global_: bool = typer.Option(False, "--global", "-g", help="Save to global config"),
    activate: str = typer.Option("", "--activate", "-a", help="Activate a provider by name"),
) -> None:
    """[Deprecated] Use `laravelgraph providers` sub-commands instead."""
    console.print(
        "[dim][yellow]Note:[/yellow] `configure` is deprecated — "
        "use [bold]laravelgraph providers add <name>[/bold] instead.[/dim]\n"
    )
    if activate:
        providers_activate(name=activate, path=path, global_=global_)
    else:
        # Show provider list and let user pick (interactive — mirrors old wizard entry)
        from laravelgraph.config import Config
        from laravelgraph.mcp.summarize import PROVIDER_REGISTRY, provider_status
        root = _project_root(path)
        cfg = Config.load(root)
        status = provider_status(cfg.llm)
        current_active = status.get("active_provider")
        configured_names = {n for n, info in status["providers"].items() if info.get("configured")}

        cloud_providers = [(n, v) for n, v in PROVIDER_REGISTRY.items() if not v.get("local")]
        local_providers = [(n, v) for n, v in PROVIDER_REGISTRY.items() if v.get("local")]
        all_providers = cloud_providers + local_providers

        console.print("\n[bold]Cloud providers:[/bold]\n")
        for i, (pname, info) in enumerate(cloud_providers, start=1):
            active_marker = " [green]← active[/green]" if pname == current_active else ""
            configured_marker = " [dim]✓ configured[/dim]" if pname in configured_names else ""
            console.print(f"  [cyan]{i:2}[/cyan]  {info['label']}{configured_marker}{active_marker}")
        console.print("\n[bold]Local providers:[/bold]\n")
        for i, (pname, info) in enumerate(local_providers, start=len(cloud_providers) + 1):
            active_marker = " [green]← active[/green]" if pname == current_active else ""
            configured_marker = " [dim]✓ configured[/dim]" if pname in configured_names else ""
            console.print(f"  [cyan]{i:2}[/cyan]  {info['label']}{configured_marker}{active_marker}")
        console.print(f"\n  [cyan] 0[/cyan]  Disable summaries\n")

        choice_str = typer.prompt("Select provider number", default="1")
        if choice_str == "0":
            cfg_path = _prompt_scope(root, global_)
            _providers_write_config(cfg_path, {"enabled": False})
            console.print("[yellow]Summaries disabled.[/yellow]")
            return
        try:
            idx = int(choice_str) - 1
            provider_name, _ = all_providers[idx]
        except (ValueError, IndexError):
            console.print(f"[red]Invalid choice:[/red] {choice_str}")
            raise typer.Exit(1)
        providers_add(name=provider_name, path=path, global_=global_)


# ── setup ─────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="4. Server & Agent Integration")
def setup(
    path: Optional[Path] = PathArg,
    claude: bool = typer.Option(False, "--claude", help="Print config for Claude Code"),
    cursor: bool = typer.Option(False, "--cursor", help="Print config for Cursor"),
    windsurf: bool = typer.Option(False, "--windsurf", help="Print config for Windsurf"),
    http: bool = typer.Option(False, "--http", help="Show HTTP/SSE remote config instead of local stdio"),
    url: str = typer.Option("", "--url", help="Remote SSE URL (e.g. http://your-ec2:3000/sse)"),
    api_key: str = typer.Option("", "--api-key", help="Bearer token for remote HTTP server"),
) -> None:
    """Print MCP configuration JSON for AI agents."""
    root = _project_root(path)

    if http or url:
        # Remote HTTP/SSE config
        _url = url or "http://your-server:3000/sse"
        if api_key:
            config = {
                "mcpServers": {
                    "laravelgraph": {
                        "type": "sse",
                        "url": _url,
                        "headers": {"Authorization": f"Bearer {api_key}"},
                        "description": f"LaravelGraph — remote code intelligence for {root.name}",
                    }
                }
            }
        else:
            config = {
                "mcpServers": {
                    "laravelgraph": {
                        "type": "sse",
                        "url": _url,
                        "description": f"LaravelGraph — remote code intelligence for {root.name}",
                    }
                }
            }
        console.print("\n[bold]Remote HTTP/SSE config[/bold] (for EC2 / shared server):")
    else:
        # Local stdio config (current default)
        config = {
            "mcpServers": {
                "laravelgraph": {
                    "type": "local",
                    "command": ["bash", "-c", f"laravelgraph serve \"{root}\""],
                    "description": f"LaravelGraph — code intelligence for {root.name}",
                }
            }
        }
        console.print("\n[bold]Local stdio config[/bold] (auto-starts MCP server):")

    if claude:
        console.print("[bold]Claude Code (~/.claude.json or .claude.json):[/bold]")
    elif cursor:
        console.print("[bold]Cursor (~/.cursor/mcp.json):[/bold]")
    elif windsurf:
        console.print("[bold]Windsurf (~/.windsurf/mcp_config.json):[/bold]")

    console.print(json.dumps(config, indent=2))

    if not http and not url:
        console.print("\n[dim]For remote/shared server config: laravelgraph setup --http --url http://your-server:3000/sse[/dim]")


# ── export ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="5. Utilities")
def export(
    path: Optional[Path] = PathArg,
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file (default: stdout)"),
) -> None:
    """Export the graph as JSON.

    Exports nodes (classes, methods, routes, Eloquent models) from the
    KuzuDB knowledge graph as a JSON document. The output includes graph
    stats and a flat list of nodes with their label and key identifiers.

    Output is written to stdout by default, or to a file with --output.

    Examples:
      laravelgraph export                          # JSON to stdout
      laravelgraph export --output graph.json      # JSON to file
      laravelgraph export /path/to/project --output /tmp/graph.json
    """
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")
    stats = db.stats()

    data: dict = {"stats": stats, "nodes": [], "edges": []}

    # Route nodes use uri instead of fqn
    _label_queries: dict[str, str] = {
        "Class_":        "MATCH (n:Class_) RETURN n.node_id AS id, n.fqn AS fqn, n.name AS name LIMIT 1000",
        "Method":        "MATCH (n:Method) RETURN n.node_id AS id, n.fqn AS fqn, n.name AS name LIMIT 1000",
        "Route":         "MATCH (n:Route) RETURN n.node_id AS id, n.uri AS fqn, n.name AS name LIMIT 1000",
        "EloquentModel": "MATCH (n:EloquentModel) RETURN n.node_id AS id, n.fqn AS fqn, n.name AS name LIMIT 1000",
    }
    for label in ["Class_", "Method", "Route", "EloquentModel"]:
        try:
            nodes = db.execute(_label_queries[label])
            data["nodes"].extend([{"label": label, **n} for n in nodes])
        except Exception:
            pass

    result = json.dumps(data, indent=2, default=str)
    if output:
        output.write_text(result)
        console.print(f"[green]✓[/green] Exported to {output}")
    else:
        print(result)


# ── guide ─────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="5. Utilities")
def guide() -> None:
    """Full command reference with examples — start here if you're new."""
    from rich.text import Text

    console.print()
    console.print(Panel(
        "[bold white]LaravelGraph — Command Reference[/bold white]\n"
        "[dim]Every command also has its own --help page with full option details.[/dim]",
        border_style="cyan",
    ))

    sections = [
        (
            "1. Setup & Indexing",
            "Run these once to get started, then again when your code changes.",
            [
                ("analyze",          "laravelgraph analyze",                                    "Index the current directory (incremental)"),
                ("analyze --full",   "laravelgraph analyze /path/to/project --full",            "Force full rebuild — use on first run or after major changes"),
                ("analyze --phases", "laravelgraph analyze --phases 14",                        "Re-run only route analysis (phase 14) — fast targeted update"),
                ("status",           "laravelgraph status",                                     "Show when the project was last indexed and how many nodes/edges"),
                ("list",             "laravelgraph list",                                       "List every project indexed on this machine"),
                ("clean",            "laravelgraph clean",                                      "Delete the index — useful to start fresh (re-analyze to rebuild)"),
            ],
        ),
        (
            "2. Explore Your Codebase",
            "Inspect routes, models, events, and any symbol directly from the terminal.",
            [
                ("query",            "laravelgraph query \"user authentication\"",              "Search for any symbol using natural language or a class name"),
                ("query --role",     "laravelgraph query Post --role model",                    "Narrow search to a specific Laravel role (model, controller, event…)"),
                ("context",          "laravelgraph context PostController",                     "See everything that calls/is called by a symbol, its routes, events, models"),
                ("context (FQN)",    "laravelgraph context \"App\\Http\\Controllers\\PostController::store\"", "Use full FQN for exact match"),
                ("routes",           "laravelgraph routes",                                     "Show all routes with HTTP method, URI, controller, and middleware"),
                ("routes --filter",  "laravelgraph routes --method GET --uri /api",             "Filter routes by HTTP method and URI pattern"),
                ("models",           "laravelgraph models",                                     "Show all Eloquent models and their hasMany/belongsTo relationships"),
                ("events",           "laravelgraph events",                                     "Show the full event → listener → queued-job dispatch chain"),
                ("bindings",         "laravelgraph bindings",                                   "Show service container bindings (which class implements which interface)"),
                ("schema",           "laravelgraph schema",                                     "Show database tables and columns parsed from migration files"),
                ("schema --table",   "laravelgraph schema --table users",                       "Show columns for a specific table only"),
            ],
        ),
        (
            "3. Code Analysis",
            "Understand the impact of changes and find code quality issues.",
            [
                ("impact",           "laravelgraph impact PostController",                      "Find every symbol that would break if you changed PostController"),
                ("impact --depth",   "laravelgraph impact PostController --depth 2",            "Control how many hops deep the blast radius search goes (default: 3)"),
                ("dead-code",        "laravelgraph dead-code",                                  "Find methods with no callers (excludes controllers/listeners/jobs by default)"),
                ("diff",             "laravelgraph diff main HEAD",                             "Show which symbols were added/changed/removed between two git branches"),
                ("diff (commits)",   "laravelgraph diff HEAD~5 HEAD",                           "Compare the last 5 commits"),
                ("cypher",           "laravelgraph cypher \"MATCH (r:Route) RETURN r.uri, r.controller_fqn LIMIT 10\"", "Run a raw Cypher query against the graph — read-only"),
            ],
        ),
        (
            "4. Server & Agent Integration",
            "Connect AI agents (Claude Code, Cursor, Windsurf) to your codebase.",
            [
                ("serve",            "laravelgraph serve",                                      "Start the MCP server over stdio — Claude Code auto-starts this"),
                ("serve --http",     "laravelgraph serve --http --host 0.0.0.0 --port 3000",   "Start HTTP/SSE server — for EC2 or shared team server"),
                ("serve --api-key",  "laravelgraph serve --http --api-key your-secret",         "Require Bearer token auth on the HTTP server"),
                ("watch",            "laravelgraph watch",                                      "Re-index automatically whenever a PHP file changes (no MCP server)"),
                ("serve --watch",    "laravelgraph serve --watch",                              "Run MCP server + live re-indexing together"),
                ("setup",            "laravelgraph setup --claude",                             "Print the MCP config JSON to paste into Claude Code settings"),
                ("setup --http",     "laravelgraph setup --http --url http://server:3000/sse", "Print remote HTTP config for EC2/shared server"),
                ("configure",        "laravelgraph configure",                                  "Interactive wizard to set up an LLM provider for semantic summaries"),
                ("providers",        "laravelgraph providers",                                  "Show which of the 18+ LLM providers are configured and active"),
            ],
        ),
        (
            "5. Utilities",
            "Health checks, exports, and version info.",
            [
                ("doctor",           "laravelgraph doctor",                                     "Full health check — config, DB, source injection, routes, LLM, transport"),
                ("export",           "laravelgraph export --output graph.json",                 "Export the full graph as JSON (nodes + edges + stats)"),
                ("version",          "laravelgraph version",                                    "Print the installed version"),
                ("guide",            "laravelgraph guide",                                      "This reference page"),
            ],
        ),
    ]

    for section_title, section_desc, commands in sections:
        console.print()
        console.print(f"[bold cyan]{section_title}[/bold cyan]")
        console.print(f"[dim]{section_desc}[/dim]")
        console.print()

        table = Table(
            show_header=False,
            box=None,
            padding=(0, 2),
            show_edge=False,
        )
        table.add_column("Example", style="bold green", no_wrap=False)
        table.add_column("What it does", style="dim")

        for _cmd, example, description in commands:
            table.add_row(example, description)

        console.print(table)

    console.print()
    console.print(Panel(
        "[dim]All commands accept [bold]--help[/bold] for full options.\n"
        "Most commands default to the current directory — pass a path to target another project.[/dim]",
        border_style="dim",
    ))
    console.print()


# ── db-connections ────────────────────────────────────────────────────────────

db_app = typer.Typer(
    name="db-connections",
    help="Manage live MySQL database connections for schema introspection.",
    rich_markup_mode="rich",
)
app.add_typer(db_app, name="db-connections", rich_help_panel="1. Setup & Indexing")


# ── agent ─────────────────────────────────────────────────────────────────────

agent_app = typer.Typer(
    name="agent",
    help="Install agent instruction files for AI tools (Claude Code, OpenCode, Cursor).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(agent_app, name="agent", rich_help_panel="4. Server & Agent Integration")


@agent_app.command(name="install")
def agent_install(
    path: Optional[Path] = PathArg,
    tool: str = typer.Option(
        "claude-code",
        "--tool", "-t",
        help="Target agent tool: claude-code | opencode | cursor | all",
    ),
) -> None:
    """Install LaravelGraph agent instructions for your AI coding tool.

    Writes a detailed protocol block to the config file that your AI agent
    reads at session start — tool hierarchy, investigation workflows, plugin
    workflow, store_discoveries protocol, and common pitfalls.

    The block is idempotent: running the command again after a LaravelGraph
    upgrade replaces the existing section in-place.

    Supported targets:
      claude-code  → CLAUDE.md  (project root)
      opencode     → .opencode/instructions.md
      cursor       → .cursorrules
      all          → all three files

    Examples:
      laravelgraph agent install .                        # Claude Code (default)
      laravelgraph agent install . --tool opencode
      laravelgraph agent install . --tool all
    """
    from laravelgraph.agent_installer import (
        install_for_claude_code,
        install_for_cursor,
        install_for_opencode,
        INSTALL_TARGETS,
    )

    root = _project_root(path)

    valid = set(INSTALL_TARGETS) | {"all"}
    if tool not in valid:
        console.print(f"[red]Unknown tool '{tool}'.[/red]  Valid: {', '.join(sorted(valid))}")
        raise typer.Exit(1)

    targets_to_run = list(INSTALL_TARGETS) if tool == "all" else [tool]
    installers = {
        "claude-code": install_for_claude_code,
        "opencode":    install_for_opencode,
        "cursor":      install_for_cursor,
    }

    for t in targets_to_run:
        fn = installers[t]
        written = fn(root)
        console.print(f"[green]✓[/green] [bold]{t}[/bold] → {written.relative_to(root)}")

    console.print()
    console.print(
        "[dim]Re-run after upgrading LaravelGraph to refresh the instructions.[/dim]"
    )


# ── plugin ─────────────────────────────────────────────────────────────────────

plugin_app = typer.Typer(
    name="plugin",
    help="Manage project-specific analysis plugins.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(plugin_app, name="plugin", rich_help_panel="5. Utilities")


# ── logs ───────────────────────────────────────────────────────────────────────

logs_app = typer.Typer(
    help="View and manage LaravelGraph logs.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
app.add_typer(logs_app, name="logs", rich_help_panel="5. Utilities")


def _parse_since(since: str) -> float:
    """Parse a 'since' string like '2h', '1d', '30m' into hours as a float."""
    since = since.strip().lower()
    if not since:
        return 0.0
    if since.endswith("h"):
        return float(since[:-1])
    if since.endswith("d"):
        return float(since[:-1]) * 24.0
    if since.endswith("m"):
        return float(since[:-1]) / 60.0
    return 0.0


@logs_app.command(name="show")
def logs_show(
    limit: int = typer.Option(50, "--limit", "-n", help="Number of entries to show"),
    level: str = typer.Option("", "--level", "-l", help="Filter by level: debug|info|warning|error"),
    tool: str = typer.Option("", "--tool", "-t", help="Filter by MCP tool name (partial match)"),
    plugin: str = typer.Option("", "--plugin", "-p", help="Filter by plugin name (partial match)"),
    since: str = typer.Option("", "--since", "-s", help="Show logs from last N hours/days (e.g. '2h', '1d', '30m')"),
    search: str = typer.Option("", "--search", help="Search text in any field"),
) -> None:
    """Show recent log entries with optional filters."""
    from laravelgraph.logging_manager import LogManager

    log_dir = Path.home() / ".laravelgraph" / "logs"
    mgr = LogManager(log_dir)

    since_hours = _parse_since(since)
    entries = mgr.get_recent(
        limit=limit,
        level=level,
        tool=tool,
        plugin=plugin,
        since_hours=since_hours,
        search=search,
    )

    if not entries:
        console.print("[yellow]No log entries found.[/yellow]")
        return

    LEVEL_STYLES = {
        "error": "red",
        "warning": "yellow",
        "info": "cyan",
        "debug": "dim",
    }

    table = Table(
        title=f"Logs ({len(entries)} entries)",
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
        pad_edge=False,
    )
    table.add_column("Time", style="dim", width=8, no_wrap=True)
    table.add_column("Level", width=8, no_wrap=True)
    table.add_column("Message", no_wrap=False)
    table.add_column("Details", style="dim", no_wrap=False)

    for entry in entries:
        ts = entry.get("timestamp") or entry.get("ts") or entry.get("time") or ""
        if ts and len(str(ts)) >= 19:
            ts = str(ts)[11:19]  # Extract HH:MM:SS
        elif ts:
            ts = str(ts)[:8]

        lvl = str(entry.get("level", "info")).lower()
        lvl_style = LEVEL_STYLES.get(lvl, "")
        lvl_cell = f"[{lvl_style}]{lvl}[/{lvl_style}]" if lvl_style else lvl

        msg = str(entry.get("message") or entry.get("msg") or entry.get("event") or "")

        # Build details from remaining keys
        skip_keys = {"timestamp", "ts", "time", "level", "message", "msg", "event", "logger"}
        detail_parts = []
        for k, v in entry.items():
            if k not in skip_keys and v is not None and v != "":
                detail_parts.append(f"{k}={v!r}"[:40])
        details = "  ".join(detail_parts[:4])

        table.add_row(ts, lvl_cell, msg[:80], details[:80])

    console.print(table)


@logs_app.command()
def tail(
    level: str = typer.Option("", "--level", "-l", help="Filter by level"),
    tool: str = typer.Option("", "--tool", "-t", help="Filter by MCP tool name (partial match)"),
    plugin: str = typer.Option("", "--plugin", "-p", help="Filter by plugin name (partial match)"),
) -> None:
    """Live tail log output (Ctrl+C to stop)."""
    from laravelgraph.logging_manager import LogManager, format_log_entry

    log_dir = Path.home() / ".laravelgraph" / "logs"
    mgr = LogManager(log_dir)

    console.print(f"[dim]Tailing logs from {log_dir} — Ctrl+C to stop[/dim]")

    def _print_entry(entry: dict) -> None:
        line = format_log_entry(entry, color=True)
        console.print(line)

    mgr.tail(callback=_print_entry, level=level, tool=tool, plugin=plugin)


@logs_app.command()
def logs_stats() -> None:
    """Show log statistics: entry counts by level and tool, disk usage."""
    from laravelgraph.logging_manager import LogManager

    log_dir = Path.home() / ".laravelgraph" / "logs"
    mgr = LogManager(log_dir)

    stats = mgr.get_stats()

    # Level counts table
    level_table = Table(title="Log Entries by Level", show_header=True, header_style="bold cyan")
    level_table.add_column("Level")
    level_table.add_column("Count", justify="right")
    LEVEL_STYLES = {"error": "red", "warning": "yellow", "info": "cyan", "debug": "dim"}
    for lvl, count in sorted(stats.get("by_level", {}).items(), key=lambda x: -x[1]):
        style = LEVEL_STYLES.get(lvl, "")
        lvl_cell = f"[{style}]{lvl}[/{style}]" if style else lvl
        level_table.add_row(lvl_cell, str(count))
    console.print(level_table)
    console.print()

    # Top tools table
    tool_table = Table(title="Top Tools by Call Count", show_header=True, header_style="bold cyan")
    tool_table.add_column("Tool")
    tool_table.add_column("Calls", justify="right")
    for tool_name, count in list(stats.get("by_tool", {}).items())[:10]:
        tool_table.add_row(tool_name, str(count))
    console.print(tool_table)
    console.print()

    # Summary
    console.print(
        f"[bold]Total entries:[/bold] {stats.get('total_entries', 0)}  "
        f"[bold]Files:[/bold] {stats.get('file_count', 0)}  "
        f"[bold]Disk:[/bold] {stats.get('disk_size_mb', 0)} MB"
    )
    if stats.get("oldest_entry"):
        console.print(f"[dim]Oldest: {stats['oldest_entry']}  Newest: {stats.get('newest_entry', '')}[/dim]")


@logs_app.command()
def logs_clear(
    all_logs: bool = typer.Option(False, "--all", help="Clear ALL logs, not just old ones"),
    days: int = typer.Option(30, "--days", help="Clear logs older than N days (default: 30)"),
) -> None:
    """Clear old log files."""
    from laravelgraph.logging_manager import LogManager

    log_dir = Path.home() / ".laravelgraph" / "logs"
    mgr = LogManager(log_dir)

    if all_logs:
        confirmed = typer.confirm("This will delete ALL log files. Are you sure?", default=False)
        if not confirmed:
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        deleted = mgr.clear_all()
        console.print(f"[green]Cleared {deleted} log file(s).[/green]")
    else:
        deleted = mgr.clear_old(days=days)
        console.print(f"[green]Cleared {deleted} log file(s) older than {days} days.[/green]")


@plugin_app.command(name="list")
def plugin_list(path: Optional[Path] = PathArg) -> None:
    """List all plugins in this project, their status, and registered tools."""
    root = _project_root(path)
    plugins_dir = root / ".laravelgraph" / "plugins"

    if not plugins_dir.exists() or not list(plugins_dir.glob("*.py")):
        console.print("[yellow]No plugins installed.[/yellow]")
        console.print("Run [bold]laravelgraph plugin suggest[/bold] to see recommendations.")
        return

    from laravelgraph.config import index_dir as _index_dir
    from laravelgraph.plugins.meta import PluginMetaStore
    from laravelgraph.plugins.validator import PluginValidationError, PluginValidator

    validator = PluginValidator()
    meta_store = PluginMetaStore(_index_dir(root))
    plugin_files = sorted(plugins_dir.glob("*.py"))

    # Compute total calls across all plugins for relative contribution scoring
    all_meta = meta_store.all()
    total_calls = max(sum(m.call_count for m in all_meta), 1)

    table = Table(title=f"Plugins — {root}", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Calls")
    table.add_column("Contribution")
    table.add_column("Health")
    table.add_column("Last Used")
    table.add_column("Notes")

    for pf in plugin_files:
        try:
            manifest, warnings = validator.validate(pf)
            name = manifest.get("name", pf.stem)
            version = manifest.get("version", "?")
        except PluginValidationError as exc:
            name = pf.stem
            version = "?"
            warnings = exc.errors

        meta = meta_store.get(name)
        meta_status = meta.status if meta else "unknown"

        # Status column
        if meta_status == "disabled":
            status_cell = "[dim]disabled ✗[/dim]"
        else:
            valid_icon = "[green]active ✓[/green]"
            status_cell = valid_icon

        # Call count
        call_count = meta.call_count if meta else 0
        calls_cell = str(call_count)

        # Contribution score — bar
        score = meta.contribution_score if meta else 0.0
        rel_pct = min(100.0, (call_count / total_calls) * 100.0)
        if rel_pct >= 50:
            bar_color = "green"
        elif rel_pct >= 20:
            bar_color = "yellow"
        else:
            bar_color = "red"
        bar_filled = int(rel_pct / 10)
        bar = f"[{bar_color}]{'█' * bar_filled}{'░' * (10 - bar_filled)}[/{bar_color}] {rel_pct:.0f}%"

        # Health indicator
        if meta and meta.call_count > 20:
            error_rate = meta.error_count / meta.call_count if meta.call_count > 0 else 0.0
            empty_rate = meta.empty_result_count / meta.call_count if meta.call_count > 0 else 0.0
            if error_rate > 0.15 or empty_rate > 0.25:
                health = "🔴 needs improvement"
            elif error_rate > 0.05 or empty_rate > 0.10:
                health = "🟡 underperforming"
            else:
                health = "🟢 healthy"
        elif meta and meta.call_count > 0:
            health = "🟢 healthy"
        else:
            health = "[dim]no data[/dim]"

        # Last used
        last_used = ""
        if meta and meta.last_used:
            last_used = str(meta.last_used)[:10]

        # Notes: system prompt indicator + warnings
        notes_parts = []
        if meta and meta.system_prompt:
            notes_parts.append("📝 prompt")
        if warnings:
            notes_parts.append(f"[yellow]{len(warnings)} warn[/yellow]")

        table.add_row(
            name,
            version,
            status_cell,
            calls_cell,
            bar,
            health,
            last_used,
            "  ".join(notes_parts),
        )

    console.print(table)


@plugin_app.command()
def validate(
    plugin_file: Path = typer.Argument(..., help="Path to plugin .py file to validate"),
) -> None:
    """Validate a plugin file against all governance rules before deploying it."""
    from laravelgraph.plugins.validator import PluginValidationError, PluginValidator

    if not plugin_file.exists():
        console.print(f"[red]File not found:[/red] {plugin_file}")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Validating:[/bold] {plugin_file}",
        title="Plugin Validation",
        border_style="cyan",
    ))

    validator = PluginValidator()
    try:
        manifest, warnings = validator.validate(plugin_file)
        console.print(f"  [green]✓[/green]  PLUGIN_MANIFEST found")
        for field in ("name", "version", "tool_prefix"):
            val = manifest.get(field, "")
            console.print(f"  [green]✓[/green]  {field}: [bold]{val}[/bold]")
        console.print(f"  [green]✓[/green]  Forbidden patterns: none")
        console.print(f"  [green]✓[/green]  Tool prefix compliance: OK")
        if warnings:
            console.print()
            for w in warnings:
                console.print(f"  [yellow]![/yellow]  {w}")
        console.print()
        console.print(Panel(
            f"[green]Plugin is valid[/green]  ({len(warnings)} warning(s))",
            border_style="green",
        ))
    except PluginValidationError as exc:
        for err in exc.errors:
            console.print(f"  [red]✗[/red]  {err}")
        for w in exc.warnings:
            console.print(f"  [yellow]![/yellow]  {w}")
        console.print()
        console.print(Panel(
            f"[red]{len(exc.errors)} error(s)[/red] — fix the issues above before deploying.",
            border_style="red",
        ))
        raise typer.Exit(1)


@plugin_app.command()
def scaffold(
    name: str = typer.Argument(..., help="Plugin name (alphanumeric + hyphens, e.g. 'payment-audit')"),
    recipe: str = typer.Option("", "--recipe", "-r", help="Base on a detected recipe (use 'plugin suggest' to see options)"),
    path: Optional[Path] = PathArg,
) -> None:
    """Generate a pre-populated plugin file from your project's graph data."""
    root = _project_root(path)

    from laravelgraph.config import Config, index_dir as _index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.plugins.scaffolder import scaffold_plugin

    db_path = _index_dir(root) / "graph.kuzu"
    if not db_path.exists():
        console.print(f"[red]No index found.[/red] Run: laravelgraph analyze {root}")
        raise typer.Exit(1)

    try:
        db = GraphDB(db_path)
    except Exception as e:
        console.print(f"[red]Could not open graph:[/red] {e}")
        raise typer.Exit(1)

    recipe_slug = recipe.strip() or None

    try:
        output_path = scaffold_plugin(name, recipe_slug, root, db)
    except FileExistsError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Scaffold failed:[/red] {e}")
        raise typer.Exit(1)

    console.print(Panel(
        f"[green]Plugin scaffolded successfully![/green]\n\n"
        f"[bold]File:[/bold] {output_path}\n\n"
        f"[bold]Next steps:[/bold]\n"
        f"  1. Edit [cyan]{output_path.name}[/cyan] and implement run() + register_tools()\n"
        f"  2. Run [bold]laravelgraph plugin validate {output_path}[/bold] to check for issues\n"
        f"  3. Run [bold]laravelgraph analyze[/bold] to execute the plugin in the pipeline\n"
        f"  4. Restart the MCP server to register the new tools",
        title="Plugin Created",
        border_style="green",
    ))


@plugin_app.command()
def suggest(path: Optional[Path] = PathArg) -> None:
    """Analyze the graph and recommend which plugins would add the most value for this project."""
    root = _project_root(path)

    from laravelgraph.config import index_dir as _index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.plugins.suggest import detect_applicable_recipes, format_suggestions

    db_path = _index_dir(root) / "graph.kuzu"
    if not db_path.exists():
        console.print(f"[red]No index found.[/red] Run: laravelgraph analyze {root}")
        raise typer.Exit(1)

    try:
        db = GraphDB(db_path)
    except Exception as e:
        console.print(f"[red]Could not open graph:[/red] {e}")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Project:[/bold] {root}",
        title="Plugin Suggestions",
        border_style="cyan",
    ))
    console.print("[dim]Scanning graph for domain patterns...[/dim]\n")

    results = detect_applicable_recipes(db)

    if not results:
        console.print("[yellow]No plugins match this project's domain patterns.[/yellow]")
        console.print(
            "\nThe suggestion engine looks for signals like payment models, tenant columns,\n"
            "booking tables, etc. If your project has these features, ensure the database\n"
            "columns and tables are indexed:\n"
            "  [bold]laravelgraph analyze --phases 24,25,26[/bold]"
        )
        return

    for i, result in enumerate(results, 1):
        from laravelgraph.plugins.suggest import PluginRecipe
        r: PluginRecipe = result["recipe"]
        matched = result["signals_matched"]
        total = result["total_signals"]
        evidence = result["evidence"]

        console.print(f"[bold cyan]{i}. {r.title}[/bold cyan]")
        console.print(f"   [dim]Signals matched: {matched}/{total}[/dim]")
        console.print(f"   {r.description}")
        if evidence:
            console.print("   [dim]Evidence:[/dim]")
            for ev in evidence[:3]:
                console.print(f"     [dim]· {ev}[/dim]")
        console.print(
            f"   [bold]Scaffold command:[/bold] "
            f"laravelgraph plugin scaffold {r.name} --recipe {r.slug}"
        )
        console.print()


@plugin_app.command()
def enable(
    name: str = typer.Argument(..., help="Plugin name"),
    path: Optional[Path] = PathArg,
) -> None:
    """Enable a disabled plugin."""
    root = _project_root(path)
    from laravelgraph.config import index_dir as _index_dir
    from laravelgraph.plugins.meta import PluginMeta, PluginMetaStore

    meta_store = PluginMetaStore(_index_dir(root))
    meta = meta_store.get(name)
    if meta is None:
        # Create entry if missing
        from datetime import datetime, timezone
        meta = PluginMeta(name=name, status="active", created_at=datetime.now(timezone.utc).isoformat())
        meta_store.set(meta)
    else:
        meta_store.enable(name)
    console.print(f"[green]Plugin '{name}' enabled.[/green]")


@plugin_app.command()
def disable(
    name: str = typer.Argument(..., help="Plugin name"),
    path: Optional[Path] = PathArg,
) -> None:
    """Disable a plugin without deleting it."""
    root = _project_root(path)
    from laravelgraph.config import index_dir as _index_dir
    from laravelgraph.plugins.meta import PluginMeta, PluginMetaStore

    meta_store = PluginMetaStore(_index_dir(root))
    meta = meta_store.get(name)
    if meta is None:
        from datetime import datetime, timezone
        meta = PluginMeta(name=name, status="disabled", created_at=datetime.now(timezone.utc).isoformat())
        meta_store.set(meta)
    else:
        meta_store.disable(name)
    console.print(f"[yellow]Plugin '{name}' disabled.[/yellow]")
    console.print("[dim]Re-enable with: laravelgraph plugin enable {name}[/dim]")


@plugin_app.command()
def delete(
    name: str = typer.Argument(..., help="Plugin name"),
    path: Optional[Path] = PathArg,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Permanently delete a plugin and all its graph data."""
    root = _project_root(path)
    from laravelgraph.config import index_dir as _index_dir
    from laravelgraph.plugins.meta import PluginMetaStore
    from laravelgraph.plugins.plugin_graph import init_plugin_graph

    if not yes:
        confirmed = typer.confirm(
            f"Permanently delete plugin '{name}' and all its graph data?",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    plugins_dir = root / ".laravelgraph" / "plugins"
    plugin_path = plugins_dir / f"{name}.py"

    removed_file = False
    if plugin_path.exists():
        plugin_path.unlink()
        removed_file = True

    # Remove from plugin graph
    try:
        plugin_db = init_plugin_graph(_index_dir(root))
        deleted_nodes = plugin_db.delete_plugin_data(name)
        plugin_db.close()
    except Exception as exc:
        deleted_nodes = 0
        console.print(f"[yellow]Warning: could not clean plugin graph:[/yellow] {exc}")

    # Remove meta
    meta_store = PluginMetaStore(_index_dir(root))
    meta_store.delete(name)

    if removed_file:
        console.print(f"[green]Plugin '{name}' deleted.[/green]")
        if deleted_nodes:
            console.print(f"[dim]Removed {deleted_nodes} plugin graph node(s).[/dim]")
    else:
        console.print(f"[yellow]Plugin file '{name}.py' not found — metadata cleared.[/yellow]")


@plugin_app.command()
def prompt(
    name: str = typer.Argument(..., help="Plugin name"),
    text: str = typer.Argument(..., help="System prompt text to attach to this plugin"),
    path: Optional[Path] = PathArg,
) -> None:
    """Attach a system prompt to a plugin (shown to agents automatically)."""
    root = _project_root(path)
    from laravelgraph.config import index_dir as _index_dir
    from laravelgraph.plugins.meta import PluginMeta, PluginMetaStore

    meta_store = PluginMetaStore(_index_dir(root))
    meta = meta_store.get(name)
    if meta is None:
        from datetime import datetime, timezone
        meta = PluginMeta(name=name, status="active", created_at=datetime.now(timezone.utc).isoformat())
    meta.system_prompt = text
    meta_store.set(meta)
    console.print(f"[green]System prompt attached to plugin '{name}'.[/green]")
    console.print(f"[dim]Prompt:[/dim] {text[:80]}{'...' if len(text) > 80 else ''}")
    console.print("[dim]The prompt will be loaded by the MCP server on next startup.[/dim]")


@plugin_app.command(name="migrate")
def plugin_migrate(path: Optional[Path] = PathArg) -> None:
    """Migrate existing plugins to the latest store_discoveries signature.

    Plugins generated before v0.3.0 have an old store_discoveries() with no
    parameters. This command upgrades them in-place to store_discoveries(findings: str)
    so agents can call them with plain-text findings immediately.

    Safe to run multiple times — already-migrated plugins are reported as up-to-date.
    """
    from rich.table import Table

    root = _project_root(path)
    plugins_dir = root / ".laravelgraph" / "plugins"

    if not plugins_dir.exists():
        console.print("[yellow]No plugins directory found at .laravelgraph/plugins/[/yellow]")
        raise typer.Exit(0)

    plugin_files = sorted(plugins_dir.glob("*.py"))
    if not plugin_files:
        console.print("[yellow]No plugins found.[/yellow]")
        raise typer.Exit(0)

    from laravelgraph.plugins.generator import migrate_plugin_store_tool
    import re as _re

    table = Table(title="Plugin Migration — store_discoveries", show_lines=True)
    table.add_column("Plugin", style="bold")
    table.add_column("Status")
    table.add_column("Notes")

    migrated_count = 0
    for plugin_path in plugin_files:
        source = plugin_path.read_text(encoding="utf-8")
        # Extract manifest fields
        name_m = _re.search(r'"name":\s*"([^"]+)"', source)
        prefix_m = _re.search(r'"tool_prefix":\s*"([^"]+)"', source)
        if not name_m or not prefix_m:
            table.add_row(plugin_path.stem, "[dim]skipped[/dim]", "No PLUGIN_MANIFEST found")
            continue

        slug = name_m.group(1)
        prefix = prefix_m.group(1)

        try:
            patched = migrate_plugin_store_tool(plugin_path, prefix, slug)
        except Exception as exc:
            table.add_row(slug, "[red]error[/red]", str(exc))
            continue

        if patched:
            migrated_count += 1
            table.add_row(slug, "[green]🔄 migrated[/green]", "store_discoveries(findings: str) upgraded")
        else:
            table.add_row(slug, "[dim]✅ up-to-date[/dim]", "")

    console.print(table)
    console.print()
    if migrated_count:
        console.print(f"[green]{migrated_count} plugin(s) migrated.[/green]")
        console.print("[dim]Restart the MCP server to load the updated tools.[/dim]")
    else:
        console.print("[dim]All plugins already up-to-date.[/dim]")
    console.print()
    console.print("[yellow]Note:[/yellow] Plugins with Cypher property errors (e.g. r.method) still need LLM regeneration:")
    console.print('[dim]  laravelgraph_update_plugin("plugin-name", "r.method should be r.http_method")[/dim]')


@plugin_app.command()
def evolve(
    path: Optional[Path] = PathArg,
    max_generate: int = typer.Option(3, "--max-generate", "-n", help="Max plugins to generate per run"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be generated without generating"),
) -> None:
    """Evolve the plugin library: detect gaps and regenerate stale plugins.

    Combines three signals to find the highest-value domains to generate:

    \b
    1. Built-in recipes (payment, tenancy, bookings, etc.)
    2. Feature node gaps — phase-27 clusters with no plugin yet
    3. Drift detection — existing plugins whose domain has changed

    Use in CI/cron to keep plugin knowledge up to date automatically:

    \b
      laravelgraph plugin evolve . --max-generate 2
    """
    from datetime import datetime, timezone

    root = _project_root(path)
    from laravelgraph.config import Config, index_dir as _index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.plugins.meta import PluginMetaStore
    from laravelgraph.plugins.self_improve import (
        auto_generate_suggested,
        check_domain_drift,
        take_domain_snapshot,
    )

    _index = _index_dir(root)
    db_path = _index / "graph.kuzu"
    if not db_path.exists():
        console.print("[red]No graph index found. Run `laravelgraph analyze` first.[/red]")
        raise typer.Exit(1)

    cfg = Config.load(root)
    db = GraphDB(db_path, force_reinit=False)
    meta_store = PluginMetaStore(_index)
    plugins_dir = root / ".laravelgraph" / "plugins"

    console.print(f"[bold]Plugin Evolution[/bold]  ({root.name})")
    console.print(f"[dim]Plugins dir:[/dim] {plugins_dir}")
    console.print()

    # ── Stage 1: drift detection ───────────────────────────────────────────────
    drifted: list[str] = []
    all_metas = meta_store.all()
    if all_metas:
        console.print("[bold]Checking existing plugins for drift...[/bold]")
        for meta in all_metas:
            if meta.status != "active":
                continue
            try:
                if check_domain_drift(db, meta):
                    drifted.append(meta.name)
                    status_tag = "[yellow]drifted[/yellow]"
                else:
                    status_tag = "[green]current[/green]"
            except Exception:
                status_tag = "[dim]unknown[/dim]"
            console.print(f"  {meta.name}: {status_tag}")
        console.print()

    # ── Stage 2: gap detection ─────────────────────────────────────────────────
    from laravelgraph.plugins.suggest import detect_feature_gaps
    gaps = detect_feature_gaps(db, meta_store, plugins_dir)

    if gaps:
        console.print("[bold]Feature gaps detected (domains with no plugin):[/bold]")
        for gap in gaps[:10]:
            console.print(
                f"  [cyan]{gap['slug']}[/cyan]  "
                f"score={gap['score']:.1f}  symbols={gap['symbol_count']}"
            )
        if len(gaps) > 10:
            console.print(f"  [dim]...and {len(gaps) - 10} more[/dim]")
        console.print()

    if drifted:
        console.print(f"[yellow]Drifted plugins:[/yellow] {', '.join(drifted)}")
        console.print()

    if not gaps and not drifted:
        console.print("[green]All plugins are current and no gaps detected.[/green]")
        return

    if dry_run:
        console.print("[dim]--dry-run: no plugins will be generated.[/dim]")
        console.print(f"Would generate up to [bold]{max_generate}[/bold] plugin(s) "
                      f"({len(gaps)} gaps, {len(drifted)} drifted).")
        return

    # ── Stage 3: generate ──────────────────────────────────────────────────────
    console.print(f"[bold]Generating up to {max_generate} plugin(s)...[/bold]")
    results = auto_generate_suggested(
        plugins_dir=plugins_dir,
        meta_store=meta_store,
        project_root=root,
        core_db=db,
        cfg=cfg,
        max_per_run=max_generate,
    )

    generated = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]

    console.print()
    if generated:
        console.print(f"[green]Generated {len(generated)} plugin(s):[/green]")
        for name, _, msg in generated:
            console.print(f"  [green]✓[/green] {name}  ({msg})")
    if failed:
        console.print(f"[yellow]Failed {len(failed)} plugin(s):[/yellow]")
        for name, _, msg in failed:
            console.print(f"  [yellow]✗[/yellow] {name}  ({msg})")
    if not results:
        console.print("[dim]No plugins generated (all in cooldown or no LLM configured).[/dim]")

    if generated:
        console.print()
        console.print("[dim]Run `laravelgraph plugin list .` to see the updated plugin library.[/dim]")
        console.print("[dim]Run `pipx reinstall laravelgraph` to activate new plugins in the MCP server.[/dim]")


def _load_db_config(root: Path) -> tuple[dict, Path]:
    """Load existing config JSON and return (data, path) for the project config."""
    import json as _json
    from laravelgraph.config import index_dir as _index_dir
    cfg_path = _index_dir(root) / "config.json"
    data: dict = {}
    if cfg_path.exists():
        try:
            data = _json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return data, cfg_path


def _save_db_config(data: dict, cfg_path: Path) -> None:
    import json as _json
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")


def _test_connection(cfg_data: dict) -> tuple[bool, str]:
    """Try to open a pymysql connection and return (success, message)."""
    try:
        import pymysql  # type: ignore[import]
    except ImportError:
        return False, "pymysql not installed. Run: pip install pymysql"

    import re as _re
    from urllib.parse import urlparse as _urlparse

    def _resolve(v: str) -> str:
        import os
        return _re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), v)

    try:
        dsn = cfg_data.get("dsn", "")
        ssl_opts = {"ssl": {}} if cfg_data.get("ssl") else None
        if dsn:
            p = _urlparse(dsn)
            conn = pymysql.connect(
                host=p.hostname or "127.0.0.1",
                port=p.port or 3306,
                user=p.username or "",
                password=_resolve(p.password or ""),
                database=(p.path or "").lstrip("/"),
                ssl=ssl_opts,
                connect_timeout=10,
            )
        else:
            conn = pymysql.connect(
                host=cfg_data.get("host", "127.0.0.1"),
                port=int(cfg_data.get("port", 3306)),
                user=cfg_data.get("username", ""),
                password=_resolve(cfg_data.get("password", "")),
                database=cfg_data.get("database", ""),
                ssl=ssl_opts,
                connect_timeout=10,
            )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()
        conn.close()
        return True, f"Connected — MySQL {version[0] if version else '?'}"
    except Exception as exc:
        return False, str(exc)


@db_app.command("list")
def db_list(
    path: Optional[Path] = PathArg,
) -> None:
    """List all configured database connections for this project."""
    root = _project_root(path)
    data, _ = _load_db_config(root)
    connections = data.get("databases", [])

    if not connections:
        console.print(
            "[yellow]No database connections configured.[/yellow]\n"
            "Run [bold]laravelgraph db-connections add[/bold] to add one."
        )
        return

    table = Table(
        title=f"Database Connections ({len(connections)})",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Name", style="bold")
    table.add_column("Driver")
    table.add_column("Host")
    table.add_column("Port", justify="right")
    table.add_column("Database")
    table.add_column("Procedures")
    table.add_column("Views")
    table.add_column("SSL")

    for c in connections:
        host = c.get("host", "") or (c.get("dsn", "")[:30] + "…" if c.get("dsn") else "—")
        table.add_row(
            c.get("name", "?"),
            c.get("driver", "mysql"),
            host,
            str(c.get("port", 3306)),
            c.get("database", "—"),
            "yes" if c.get("analyze_procedures", True) else "no",
            "yes" if c.get("analyze_views", True) else "no",
            "yes" if c.get("ssl", False) else "no",
        )

    console.print(table)
    console.print(
        "\nRun [bold]laravelgraph db-connections test[/bold] to verify connectivity.\n"
        "Run [bold]laravelgraph analyze --full[/bold] to re-index with live schema."
    )


@db_app.command("add")
def db_add(
    path: Optional[Path] = PathArg,
    name: str = typer.Option("", "--name", "-n", help="Connection name (e.g. default, analytics)"),
    no_test: bool = typer.Option(False, "--no-test", help="Skip connection test after adding"),
) -> None:
    """Add a MySQL database connection — interactive wizard.

    Prompts for connection details, tests the connection, then saves to the
    project config at [cyan]<project>/.laravelgraph/config.json[/cyan].

    Passwords may reference environment variables using [cyan]${VAR_NAME}[/cyan]
    syntax — the raw reference is stored (not the resolved value) so secrets
    are never written to disk.

    After adding, run [bold]laravelgraph analyze --full[/bold] to rebuild the
    index with live database schema.

    Examples:
      laravelgraph db-connections add
      laravelgraph db-connections add --name analytics --no-test
    """
    root = _project_root(path)
    data, cfg_path = _load_db_config(root)
    existing_names = {c.get("name", "") for c in data.get("databases", [])}

    console.print(Panel(
        "Add a [bold]live MySQL connection[/bold] for database schema introspection.\n\n"
        "The tool will connect to your database and read [cyan]information_schema[/cyan]\n"
        "to extract tables, columns, foreign keys, stored procedures, and views.\n\n"
        "[yellow]Tip:[/yellow] Use [cyan]${ENV_VAR}[/cyan] for passwords to avoid storing\n"
        "secrets in config files — e.g. [dim]${DB_PASSWORD}[/dim]",
        title="LaravelGraph — Add Database Connection",
        border_style="cyan",
    ))

    # ── Connection name ───────────────────────────────────────────────────────
    if not name:
        default_name = "default" if "default" not in existing_names else "db2"
        name = typer.prompt("Connection name (matches Laravel connection key)", default=default_name)

    if name in existing_names:
        overwrite = typer.confirm(
            f"Connection '{name}' already exists. Overwrite?", default=False
        )
        if not overwrite:
            console.print("Cancelled.")
            raise typer.Exit(0)

    # ── Input mode ────────────────────────────────────────────────────────────
    console.print("\n[bold]How would you like to enter the connection details?[/bold]\n")
    console.print("  [cyan]1[/cyan]  Individual fields  (host, port, database, username, password)")
    console.print("  [cyan]2[/cyan]  Full DSN string    (mysql://user:pass@host:3306/dbname)\n")
    mode = typer.prompt("Select", default="1")

    cfg_entry: dict = {"name": name, "driver": "mysql"}

    if mode == "2":
        dsn = typer.prompt("DSN", default="mysql://user:password@host:3306/database")
        cfg_entry["dsn"] = dsn
    else:
        cfg_entry["host"] = typer.prompt("Host", default="127.0.0.1")
        cfg_entry["port"] = int(typer.prompt("Port", default="3306"))
        cfg_entry["database"] = typer.prompt("Database (schema name)")
        cfg_entry["username"] = typer.prompt("Username")
        pw = typer.prompt(
            "Password  [dim](use ${VAR_NAME} to reference an env var)[/dim]",
            hide_input=True,
        )
        cfg_entry["password"] = pw

    # ── SSL ───────────────────────────────────────────────────────────────────
    ssl = typer.confirm("Enable SSL? (recommended for AWS RDS)", default=True)
    cfg_entry["ssl"] = ssl

    # ── Options ───────────────────────────────────────────────────────────────
    console.print()
    cfg_entry["analyze_procedures"] = typer.confirm(
        "Introspect stored procedures?", default=True
    )
    cfg_entry["analyze_views"] = typer.confirm(
        "Introspect views?", default=True
    )
    cfg_entry["analyze_triggers"] = typer.confirm(
        "Introspect triggers? (slower, off by default)", default=False
    )

    # ── Test connection ───────────────────────────────────────────────────────
    if not no_test:
        console.print("\nTesting connection...", end=" ")
        ok, msg = _test_connection(cfg_entry)
        if ok:
            console.print(f"[green]✓[/green] {msg}")
        else:
            console.print(f"[red]✗[/red] {msg}")
            save_anyway = typer.confirm("Connection test failed. Save anyway?", default=False)
            if not save_anyway:
                console.print("Cancelled. Check your connection details and try again.")
                raise typer.Exit(1)

    # ── Save ──────────────────────────────────────────────────────────────────
    connections = data.setdefault("databases", [])
    connections = [c for c in connections if c.get("name") != name]
    connections.append(cfg_entry)
    data["databases"] = connections

    _save_db_config(data, cfg_path)

    console.print(
        f"\n[green]✓[/green] Connection [bold]{name}[/bold] saved to "
        f"[cyan]{cfg_path}[/cyan]\n\n"
        "[bold]Next steps:[/bold]\n"
        "  1. [dim]laravelgraph analyze --full[/dim]  — rebuild index with live DB schema\n"
        "  2. [dim]laravelgraph db-connections list[/dim]  — verify all connections\n"
        "  3. [dim]laravelgraph db-connections test[/dim]  — re-test connectivity\n\n"
        "[yellow]Note:[/yellow] Schema changes require [bold]--full[/bold] rebuild to take effect."
    )


@db_app.command("remove")
def db_remove(
    conn_name: str = typer.Argument(..., help="Connection name to remove"),
    path: Optional[Path] = PathArg,
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Remove a configured database connection by name."""
    root = _project_root(path)
    data, cfg_path = _load_db_config(root)
    connections = data.get("databases", [])

    match = next((c for c in connections if c.get("name") == conn_name), None)
    if not match:
        console.print(f"[red]Connection not found:[/red] {conn_name}")
        console.print("Run [bold]laravelgraph db-connections list[/bold] to see configured connections.")
        raise typer.Exit(1)

    if not force:
        confirmed = typer.confirm(f"Remove connection '{conn_name}'?", default=False)
        if not confirmed:
            console.print("Cancelled.")
            return

    data["databases"] = [c for c in connections if c.get("name") != conn_name]
    _save_db_config(data, cfg_path)

    console.print(
        f"[green]✓[/green] Connection [bold]{conn_name}[/bold] removed.\n"
        "Run [bold]laravelgraph analyze --full[/bold] to rebuild the index."
    )


@db_app.command("test")
def db_test(
    conn_name: str = typer.Argument("", help="Connection name to test (default: all)"),
    path: Optional[Path] = PathArg,
) -> None:
    """Test connectivity for one or all configured database connections.

    Examples:
      laravelgraph db-connections test           # test all
      laravelgraph db-connections test analytics # test one
    """
    root = _project_root(path)
    data, _ = _load_db_config(root)
    connections = data.get("databases", [])

    if not connections:
        console.print(
            "[yellow]No connections configured.[/yellow]\n"
            "Run [bold]laravelgraph db-connections add[/bold] to add one."
        )
        raise typer.Exit(1)

    if conn_name:
        connections = [c for c in connections if c.get("name") == conn_name]
        if not connections:
            console.print(f"[red]Connection not found:[/red] {conn_name}")
            raise typer.Exit(1)

    table = Table(title="Connection Tests", show_header=True, header_style="bold")
    table.add_column("Name", style="bold")
    table.add_column("Host")
    table.add_column("Database")
    table.add_column("Status")
    table.add_column("Details")

    all_ok = True
    for c in connections:
        host = c.get("host", c.get("dsn", "")[:30] or "—")
        ok, msg = _test_connection(c)
        status = "[green]✓ OK[/green]" if ok else "[red]✗ FAIL[/red]"
        if not ok:
            all_ok = False
        table.add_row(c.get("name", "?"), host, c.get("database", "—"), status, msg)

    console.print(table)

    if not all_ok:
        raise typer.Exit(1)


# ── db-query ──────────────────────────────────────────────────────────────────

@app.command(name="db-query", rich_help_panel="1. Setup & Indexing")
def db_query(
    sql: str = typer.Argument("", help="Read-only SQL — SELECT, SHOW, DESCRIBE, or EXPLAIN"),
    path: Optional[Path] = ProjectOpt,
    connection: str = typer.Option("", "--connection", "-c", help="Connection name (default: first configured)"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows (default 50, max 500)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache and force a live query"),
    clear_cache: bool = typer.Option(False, "--clear-cache", help="Clear all cached query results and exit"),
    warm: bool = typer.Option(False, "--warm", help="Pre-warm the query cache for top-accessed and small lookup tables, then exit"),
    top_n: int = typer.Option(20, "--top-n", help="Number of top-accessed tables to warm (used with --warm)"),
    lookup_threshold: int = typer.Option(500, "--lookup-threshold", help="Tables with fewer rows than this are treated as lookup tables (used with --warm)"),
) -> None:
    """Run a read-only SQL query against a configured live database.

    Only SELECT, SHOW, DESCRIBE, and EXPLAIN are allowed — this tool is
    strictly read-only.  Results are cached for 5 minutes by default so
    repeated calls (e.g. from an AI agent during a session) don't hammer
    the database.

    Examples:
      laravelgraph db-query "SELECT * FROM course_delivery_types"
      laravelgraph db-query "DESCRIBE users"
      laravelgraph db-query "SELECT id, name FROM plans LIMIT 5" --connection staging
      laravelgraph db-query "SELECT * FROM orders WHERE status='pending'" --no-cache
      laravelgraph db-query --clear-cache
      laravelgraph db-query --warm
      laravelgraph db-query --warm --top-n 30 --lookup-threshold 1000
    """
    root = _project_root(path)

    from laravelgraph.config import Config, index_dir as _index_dir
    from laravelgraph.mcp.query_cache import QueryResultCache, validate_sql, _MAX_ROWS

    cfg = Config.load(root)
    qc = QueryResultCache(_index_dir(root))

    # ── --clear-cache ──────────────────────────────────────────────────────────
    if clear_cache:
        removed = qc.clear_all()
        console.print(f"[green]✓[/green] Query cache cleared ({removed} entries removed)")
        return

    # ── --warm ─────────────────────────────────────────────────────────────────
    if warm:
        if not cfg.databases:
            console.print("[yellow]No DB connections configured.[/yellow] Run: laravelgraph db-connections add")
            raise typer.Exit(1)
        console.print(f"[dim]Warming query cache (top {top_n} accessed + lookup tables < {lookup_threshold} rows)...[/dim]")
        try:
            from laravelgraph.mcp.warm_queries import warm_query_cache
            totals = warm_query_cache(root, cfg, top_n=top_n, lookup_threshold=lookup_threshold)
            console.print(
                f"[green]✓[/green] Cache warmed: "
                f"[bold]{totals['warmed']}[/bold] tables cached, "
                f"{totals['skipped']} already live, "
                f"{totals['errors']} errors"
            )
        except Exception as e:
            console.print(f"[red]Warm failed:[/red] {e}")
            raise typer.Exit(1)
        return

    # ── Safety check ──────────────────────────────────────────────────────────
    if not sql:
        console.print("[red]Error:[/red] Provide a SQL query or use --warm / --clear-cache")
        raise typer.Exit(1)

    err = validate_sql(sql)
    if err:
        console.print(f"[red]SQL rejected:[/red] {err}")
        raise typer.Exit(1)

    # ── Resolve connection ─────────────────────────────────────────────────────
    db_configs = cfg.databases
    if not db_configs:
        console.print(
            "[red]No database connections configured.[/red]\n"
            "Run [bold]laravelgraph db-connections add[/bold] first."
        )
        raise typer.Exit(1)

    if connection:
        conn_cfg = next((c for c in db_configs if c.name == connection), None)
        if not conn_cfg:
            names = ", ".join(c.name for c in db_configs)
            console.print(f"[red]Connection '{connection}' not found.[/red] Available: {names}")
            raise typer.Exit(1)
    else:
        conn_cfg = db_configs[0]

    conn_name = conn_cfg.name
    safe_limit = max(1, min(limit, _MAX_ROWS))

    sql_for_exec = sql.strip().rstrip(";")
    if sql_for_exec.upper().lstrip().startswith("SELECT") and \
            "LIMIT" not in sql_for_exec.upper():
        sql_for_exec = f"{sql_for_exec} LIMIT {safe_limit}"

    ttl = conn_cfg.query_cache_ttl

    # ── Cache lookup ───────────────────────────────────────────────────────────
    cache_key = qc.make_key(conn_name, sql_for_exec)
    if not no_cache and ttl > 0:
        cached = qc.get(cache_key, ttl=ttl)
        if cached:
            age = int(time.time() - cached["cached_at"])
            _render_query_table(console, cached["columns"], cached["rows"], conn_name, from_cache=True, cache_age=age)
            return

    # ── Live query ─────────────────────────────────────────────────────────────
    try:
        import pymysql  # noqa
    except ImportError:
        console.print("[red]PyMySQL not installed.[/red] Run: pip install pymysql")
        raise typer.Exit(1)

    with console.status(f"[dim]Querying {conn_name}…[/dim]"):
        try:
            from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql
            mysql_conn = _connect_mysql(conn_cfg)
            with mysql_conn.cursor() as cur:
                cur.execute(sql_for_exec)
                raw_rows = cur.fetchall()
                columns = [d[0] for d in (cur.description or [])]
            mysql_conn.close()
        except Exception as exc:
            console.print(f"[red]Query failed:[/red] {exc}")
            raise typer.Exit(1)

    rows = [dict(zip(columns, row)) for row in raw_rows]

    if ttl > 0:
        qc.set(cache_key, sql, conn_name, columns, rows, ttl=ttl)

    _render_query_table(console, columns, rows, conn_name, from_cache=False)


def _render_query_table(
    console: Console,
    columns: list,
    rows: list,
    connection: str,
    from_cache: bool = False,
    cache_age: int = 0,
) -> None:
    """Render query results as a Rich table in the terminal."""
    import time as _time
    source = f"cached {cache_age}s ago" if from_cache else "live"
    console.print(f"\n[dim]Connection:[/dim] [bold]{connection}[/bold]  [dim]({source})[/dim]")

    if not rows:
        console.print("[dim]No rows returned.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None)
    for col in columns:
        table.add_column(str(col))
    for row in rows:
        table.add_row(*[str(row.get(c, "")) for c in columns])

    console.print(table)
    console.print(f"[dim]{len(rows)} row(s)[/dim]")


# ── version ───────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="5. Utilities")
def version() -> None:
    """Print version information."""
    from laravelgraph import __version__
    console.print(f"LaravelGraph v{__version__}")


# ── changelog ─────────────────────────────────────────────────────────────────

def _find_changelog() -> Path | None:
    """Locate CHANGELOG.md — works in both dev (pip install -e) and installed (pipx) modes."""
    # Bundled inside the package (installed via pipx / pip install)
    pkg_path = Path(__file__).parent / "CHANGELOG.md"
    if pkg_path.exists():
        return pkg_path
    # Source checkout — one level up from laravelgraph/
    repo_path = Path(__file__).parent.parent / "CHANGELOG.md"
    if repo_path.exists():
        return repo_path
    return None


def _parse_changelog(text: str) -> list[dict]:
    """
    Parse Keep-a-Changelog markdown into a list of version dicts:
      [{"label": "0.2.0", "date": "2026-03-27", "latest": True, "content": "..."}]
    The first entry with a version number is marked latest=True.
    The special [Unreleased] section is included if it has content.
    """
    import re
    sections: list[dict] = []
    # Split on lines that start with "## ["
    heading_re = re.compile(r"^(## \[.+?\].*?)$", re.MULTILINE)
    parts = heading_re.split(text)
    # parts = [preamble, heading1, body1, heading2, body2, ...]
    i = 1
    found_versioned = False
    while i < len(parts) - 1:
        heading = parts[i].strip()
        body = parts[i + 1].strip()
        i += 2
        # heading looks like: ## [0.2.0] - 2026-03-27  or  ## [Unreleased]
        m = re.match(r"## \[(.+?)\](?:\s*-\s*(.+))?", heading)
        if not m:
            continue
        label = m.group(1)
        date = (m.group(2) or "").strip()
        if label.lower() == "unreleased":
            if body:
                sections.append({"label": "Unreleased", "date": "", "latest": False, "content": body})
        else:
            is_latest = not found_versioned
            found_versioned = True
            sections.append({"label": label, "date": date, "latest": is_latest, "content": body})
    return sections


def _render_changelog_sections(sections: list[dict]) -> None:
    """Render parsed changelog sections to the console with Rich."""
    from rich.markdown import Markdown
    from rich.rule import Rule

    for sec in sections:
        label = sec["label"]
        date = sec["date"]
        is_latest = sec["latest"]

        # Build header line
        if label == "Unreleased":
            title = "[bold yellow]Unreleased[/bold yellow]"
            border = "yellow"
        elif is_latest:
            title = f"[bold green]v{label}[/bold green]  [dim]{date}[/dim]  [green]← latest[/green]"
            border = "green"
        else:
            title = f"[bold]v{label}[/bold]  [dim]{date}[/dim]"
            border = "dim"

        console.print(Panel(Markdown(sec["content"]), title=title, border_style=border, padding=(0, 1)))
        console.print()


@app.command(rich_help_panel="5. Utilities")
def changelog(
    latest: bool = typer.Option(False, "--latest", "-l", help="Show only the latest release"),
    version_filter: Optional[str] = typer.Option(
        None, "--version", "-v", metavar="VERSION",
        help="Show a specific version (e.g. '0.2.0')"
    ),
    all_versions: bool = typer.Option(False, "--all", "-a", help="Show the complete release history"),
) -> None:
    """Show the release changelog in the terminal.

    By default, displays the two most recent versions.
    Use --latest for just the newest release, --all for the full history,
    or --version to jump to a specific release.

    Examples:
      laravelgraph changelog               # two most recent versions
      laravelgraph changelog --latest      # newest version only
      laravelgraph changelog --version 0.1.0
      laravelgraph changelog --all         # full history
    """
    cl_path = _find_changelog()
    if cl_path is None:
        console.print("[red]CHANGELOG.md not found.[/red]")
        raise typer.Exit(1)

    text = cl_path.read_text(encoding="utf-8")
    sections = _parse_changelog(text)

    if not sections:
        console.print("[yellow]Changelog is empty.[/yellow]")
        return

    from laravelgraph import __version__
    console.print(
        Panel(
            f"[bold green]LaravelGraph[/bold green] Changelog  "
            f"[dim](current: v{__version__})[/dim]",
            border_style="green",
            padding=(0, 1),
        )
    )
    console.print()

    if version_filter:
        matched = [s for s in sections if s["label"] == version_filter]
        if not matched:
            available = ", ".join(s["label"] for s in sections)
            console.print(f"[red]Version '{version_filter}' not found.[/red]  Available: {available}")
            raise typer.Exit(1)
        _render_changelog_sections(matched)
    elif latest:
        # First versioned section (skip Unreleased)
        versioned = [s for s in sections if s["label"] != "Unreleased"]
        _render_changelog_sections(versioned[:1])
    elif all_versions:
        _render_changelog_sections(sections)
    else:
        # Default: show Unreleased (if non-empty) + 2 most recent versioned releases
        unreleased = [s for s in sections if s["label"] == "Unreleased"]
        versioned = [s for s in sections if s["label"] != "Unreleased"]
        _render_changelog_sections(unreleased + versioned[:2])


# ── doctor ────────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="5. Utilities")
def doctor(path: Optional[Path] = PathArg) -> None:
    """Full health check — use this first when something seems wrong.

    Runs a comprehensive diagnostic and reports pass/warn/fail for each check:

      1. Config              — load config from all sources, check for errors
      2. Dependencies        — required packages (kuzu, fastmcp) and optional SDKs
      3. Graph DB            — verify KuzuDB is accessible, report total node/edge counts
      4. Index Health        — per-type symbol counts (routes, models, events, jobs) +
                               critical edge counts (CALLS, DISPATCHES, LISTENS_TO,
                               QUERIES_TABLE) + index age warning
      5. Context Quality     — source readability, summary cache read/write
      6. Data Quality        — route resolution %, event listener coverage %,
                               model-table link %, source readability
      7. Transport & Server  — binary in PATH, HTTP server reachability, config snippets
      8. LLM Provider        — active provider, model selection, live summary test
      9. Optional Features   — watchfiles (watch mode), fastembed (vector search)
     10. Database Connections — PyMySQL, configured connections, connectivity tests,
                               phases 19/24/25/26 health, query cache stats
     11. Plugins             — validate installed plugins, check manifests and tool prefixes

    Common failure patterns:
      Events: 0    → run: laravelgraph analyze --full  (events missing = stale index)
      CALLS: 0     → run: laravelgraph analyze --full  (request_flow will be shallow)
      Jobs: 0      → check if jobs exist; if they do, run: laravelgraph analyze --full
      Routes: 0    → run: laravelgraph analyze

    Exits with code 1 if any check fails.

    Examples:
      laravelgraph doctor
      laravelgraph doctor /path/to/project
    """
    import time
    root = _project_root(path)

    console.print(Panel(
        f"[bold]Project:[/bold] {root}",
        title="LaravelGraph Health Check",
        border_style="cyan",
    ))

    passes = 0
    failures = 0

    def ok(msg: str) -> None:
        nonlocal passes
        passes += 1
        console.print(f"  [green]✓[/green]  {msg}")

    def fail(msg: str, detail: str = "") -> None:
        nonlocal failures
        failures += 1
        console.print(f"  [red]✗[/red]  {msg}")
        if detail:
            console.print(f"       [dim]{detail}[/dim]")

    def warn(msg: str) -> None:
        console.print(f"  [yellow]![/yellow]  {msg}")

    def section(title: str) -> None:
        console.print(f"\n[bold]{title}[/bold]")

    # ── 1. Config ─────────────────────────────────────────────────────────────
    section("Config")
    try:
        from laravelgraph.config import Config, index_dir as _index_dir
        cfg = Config.load(root)
        ok(f"Config loaded")
    except Exception as e:
        fail("Config failed to load", str(e))
        raise typer.Exit(1)

    # ── 2. Dependencies ───────────────────────────────────────────────────────
    section("Dependencies")
    deps = {
        "kuzu":    "Graph database (required)",
        "fastmcp": "MCP server (required)",
        "typer":   "CLI (required)",
        "rich":    "Terminal UI (required)",
        "anthropic": "Anthropic SDK (optional — for Claude summaries)",
        "openai":    "OpenAI SDK (optional — for OpenAI/OpenRouter/Ollama summaries)",
    }
    for pkg, desc in deps.items():
        try:
            __import__(pkg)
            ok(f"{pkg}")
        except ImportError:
            if "required" in desc:
                fail(f"{pkg} not installed — {desc}", f"pip install {pkg}")
            else:
                warn(f"{pkg} not installed — {desc}")

    # ── 3. Graph DB ───────────────────────────────────────────────────────────
    section("Graph DB")
    db = None
    try:
        from laravelgraph.config import index_dir as _index_dir
        from laravelgraph.core.graph import GraphDB
        db_path = _index_dir(root) / "graph.kuzu"
        if not db_path.exists():
            fail("No index found", f"Run: laravelgraph analyze {root}")
        else:
            db = GraphDB(db_path)
            stats = db.stats()
            nodes = stats.get("total_nodes", 0)
            edges = stats.get("total_edges", 0)
            ok(f"Graph DB accessible — {nodes:,} nodes, {edges:,} edges")
    except Exception as e:
        err_str = str(e)
        if "Could not set lock" in err_str or "lock" in err_str.lower():
            warn("Graph DB locked — MCP server already has it open (this is normal)")
            console.print("       [dim]Stop the MCP server first if you need a full doctor check.[/dim]")
            # Treat as non-fatal: DB is working, just busy
        else:
            fail("Graph DB error", err_str)

    # ── 4. Index Health ───────────────────────────────────────────────────────
    section("Index Health")
    if db is None:
        warn("Skipping index health checks — no graph DB")
    else:
        def _count(query: str) -> int:
            try:
                rows = db.execute(query)
                return rows[0].get(list(rows[0].keys())[0], 0) if rows else 0
            except Exception:
                return -1

        # Core symbol counts
        n_routes   = _count("MATCH (r:Route) RETURN count(r) AS n")
        n_models   = _count("MATCH (m:EloquentModel) RETURN count(m) AS n")
        n_events   = _count("MATCH (e:Event) RETURN count(e) AS n")
        n_jobs     = _count("MATCH (j:Job) RETURN count(j) AS n")
        n_classes  = _count("MATCH (c:Class_) RETURN count(c) AS n")
        n_methods  = _count("MATCH (m:Method) RETURN count(m) AS n")

        # Edge counts — these power the key tools
        n_calls      = _count("MATCH ()-[r:CALLS]->() RETURN count(r) AS n")
        n_dispatches = _count("MATCH ()-[r:DISPATCHES]->() RETURN count(r) AS n")
        n_listens    = _count("MATCH ()-[r:LISTENS_TO]->() RETURN count(r) AS n")
        n_queries_t  = _count("MATCH ()-[r:QUERIES_TABLE]->() RETURN count(r) AS n")

        # Index age
        import datetime as _dt
        _idx_path = _index_dir(root) / "graph.kuzu"
        try:
            _mtime = _dt.datetime.fromtimestamp(_idx_path.stat().st_mtime)
            _age_days = (_dt.datetime.now() - _mtime).days
            _age_str = f"{_age_days}d ago" if _age_days > 0 else "today"
        except Exception:
            _age_str = "unknown"

        # Report
        if n_routes > 0:
            ok(f"Routes: {n_routes:,}")
        else:
            fail("Routes: 0 — run: laravelgraph analyze", "request_flow and feature_context will not work")

        if n_models > 0:
            ok(f"Eloquent models: {n_models:,}")
        else:
            warn("Eloquent models: 0 — run: laravelgraph analyze")

        if n_events > 0:
            ok(f"Events: {n_events:,}")
        else:
            fail(
                "Events: 0 — event dispatch graph will be empty",
                "Run: laravelgraph analyze --full  (events are indexed in phases 12-15)",
            )

        if n_jobs > 0:
            ok(f"Jobs: {n_jobs:,}")
        else:
            warn("Jobs: 0 — queued jobs will not appear in feature_context or request_flow")

        ok(f"Classes: {n_classes:,}   Methods: {n_methods:,}")

        if n_calls > 0:
            ok(f"CALLS edges: {n_calls:,} — request_flow deep traversal will work")
        else:
            fail(
                "CALLS edges: 0 — request_flow will only show controller, not service layer",
                "Run: laravelgraph analyze --full",
            )

        if n_dispatches > 0:
            ok(f"DISPATCHES edges: {n_dispatches:,}")
        else:
            warn("DISPATCHES edges: 0 — events/jobs will not appear in request_flow or feature_context")

        if n_listens > 0:
            ok(f"LISTENS_TO edges: {n_listens:,}")
        else:
            warn("LISTENS_TO edges: 0 — event listener chains will be empty")

        if n_queries_t > 0:
            ok(f"QUERIES_TABLE edges: {n_queries_t:,} — DB access tracing available")
        else:
            warn("QUERIES_TABLE edges: 0 — run: laravelgraph analyze --phases 26")

        if _age_str != "unknown" and _age_days > 7:
            warn(f"Index is {_age_str} old — consider re-running laravelgraph analyze")
        else:
            ok(f"Index last updated: {_age_str}")

    # ── 5. Context Quality ────────────────────────────────────────────────────
    section("Context Quality")
    if db is None:
        warn("Skipping context quality checks — no graph DB")
    else:
        # 5a. Source injection: find any node with a file_path and verify source reads back
        try:
            sample_nodes = db.execute(
                "MATCH (n) WHERE n.file_path IS NOT NULL AND n.line_start IS NOT NULL "
                "RETURN n.file_path AS fp, n.line_start AS ls, n.line_end AS le, n.fqn AS fqn LIMIT 1"
            )
            if sample_nodes:
                from laravelgraph.mcp.explain import read_source_snippet
                node = sample_nodes[0]
                fp, ls, le = node.get("fp"), node.get("ls"), node.get("le")
                le = le or (ls + 50 if ls else None)  # line_end may be None for some nodes
                snippet = read_source_snippet(fp, ls, le, root) if fp and ls else None
                if snippet and snippet.strip():
                    ok(f"Source injection working [dim]({len(snippet.splitlines())} lines from {Path(fp).name})[/dim]")
                else:
                    fail(
                        "Source injection broken — no source returned for node",
                        f"File: {fp}  Lines: {ls}–{le}  Run `laravelgraph analyze` to re-index",
                    )
            else:
                warn("No nodes with file_path found — run laravelgraph analyze")
        except Exception as e:
            fail("Source injection check failed", str(e))

        # 5b. Summary cache: verify the cache file is readable and returns correct structure
        try:
            from laravelgraph.mcp.cache import SummaryCache
            from laravelgraph.config import index_dir
            cache_dir = index_dir(root)
            sc = SummaryCache(cache_dir)
            _test_id = "__doctor_test__"
            sc.set(_test_id, "test summary", "doctor", file_path=None)
            val = sc.get(_test_id, file_path=None)
            if val == "test summary":
                stats = sc.stats()
                ok(f"Summary cache read/write working [dim]({stats.get('cached_summaries', 0)} cached)[/dim]")
            else:
                fail("Summary cache round-trip mismatch", f"wrote 'test summary', got {val!r}")
        except Exception as e:
            fail("Summary cache check failed", str(e))

        # 5c. Closure route detection: check if any Closure routes exist and are handled
        try:
            closure_routes = db.execute(
                "MATCH (r:Route) WHERE r.controller_fqn IS NULL OR r.controller_fqn = 'Closure' "
                "RETURN count(r) AS cnt"
            )
            cnt = closure_routes[0].get("cnt", 0) if closure_routes else 0
            if cnt > 0:
                ok(f"Closure routes detected: {cnt} route(s) — handled gracefully in request_flow")
            else:
                ok("No Closure routes in this project")
        except Exception as e:
            warn(f"Closure route check skipped: {e}")

    # ── 6. Data Quality ───────────────────────────────────────────────────────
    section("Data Quality")
    if db is None:
        warn("Skipping data quality checks — no graph DB")
    else:
        # 6a. Route resolution: what % of routes have a controller resolved
        try:
            total_r   = db.execute("MATCH (r:Route) RETURN count(r) AS cnt")
            resolved_r = db.execute(
                "MATCH (r:Route) WHERE r.controller_fqn IS NOT NULL AND r.controller_fqn <> '' "
                "AND r.controller_fqn <> 'Closure' RETURN count(r) AS cnt"
            )
            total_cnt    = total_r[0].get("cnt", 0) if total_r else 0
            resolved_cnt = resolved_r[0].get("cnt", 0) if resolved_r else 0
            if total_cnt > 0:
                pct = resolved_cnt / total_cnt * 100
                if pct >= 80:
                    ok(f"Route resolution: {resolved_cnt}/{total_cnt} ({pct:.0f}%) have a controller")
                elif pct >= 50:
                    warn(f"Route resolution: {resolved_cnt}/{total_cnt} ({pct:.0f}%) — some Closure/unresolved routes")
                else:
                    fail(
                        f"Route resolution low: {resolved_cnt}/{total_cnt} ({pct:.0f}%)",
                        "Run: laravelgraph analyze --phases 14",
                    )
        except Exception as e:
            warn(f"Route resolution check skipped: {e}")

        # 6b. Event listener coverage: events with at least one listener
        try:
            total_ev   = db.execute("MATCH (e:Event) RETURN count(e) AS cnt")
            bound_ev   = db.execute(
                "MATCH (e:Event) WHERE EXISTS { MATCH ()-[:LISTENS_TO]->(e) } RETURN count(e) AS cnt"
            )
            total_ev_cnt = total_ev[0].get("cnt", 0) if total_ev else 0
            bound_ev_cnt = bound_ev[0].get("cnt", 0) if bound_ev else 0
            if total_ev_cnt > 0:
                pct_ev = bound_ev_cnt / total_ev_cnt * 100
                if pct_ev >= 50:
                    ok(f"Event listeners: {bound_ev_cnt}/{total_ev_cnt} events ({pct_ev:.0f}%) have listeners bound")
                else:
                    warn(f"Event listeners: only {bound_ev_cnt}/{total_ev_cnt} events ({pct_ev:.0f}%) have listeners — EventServiceProvider may need re-indexing")
            else:
                warn("No events indexed — feature_context will show empty event chains")
        except Exception as e:
            warn(f"Event listener check skipped: {e}")

        # 6c. Model-table link coverage
        try:
            total_m  = db.execute("MATCH (m:EloquentModel) RETURN count(m) AS cnt")
            linked_m = db.execute(
                "MATCH (m:EloquentModel) WHERE EXISTS { MATCH (m)-[:USES_TABLE]->() } RETURN count(m) AS cnt"
            )
            total_m_cnt  = total_m[0].get("cnt", 0) if total_m else 0
            linked_m_cnt = linked_m[0].get("cnt", 0) if linked_m else 0
            if total_m_cnt > 0:
                pct_m = linked_m_cnt / total_m_cnt * 100
                if pct_m >= 70:
                    ok(f"Model-table links: {linked_m_cnt}/{total_m_cnt} models ({pct_m:.0f}%) linked to a DB table")
                else:
                    warn(
                        f"Model-table links: {linked_m_cnt}/{total_m_cnt} models ({pct_m:.0f}%) linked — "
                        "run: laravelgraph analyze --phases 19,24,25"
                    )
        except Exception as e:
            warn(f"Model-table link check skipped: {e}")

        # 6d. Scheduler status — detect commented-out Kernel.php schedules
        try:
            from laravelgraph.core.registry import Registry as _DocReg
            _doc_entry = _DocReg().get(root)
            _doc_stats = _doc_entry.stats if _doc_entry else {}
            _sched_disabled = bool(_doc_stats.get("scheduler_disabled", False))
            _sched_commented = int(_doc_stats.get("scheduler_commented_tasks", 0))
            _sched_active = int(_doc_stats.get("scheduled_tasks", 0))
            if _sched_disabled:
                fail(
                    f"Scheduler disabled — {_sched_commented} task(s) commented out, 0 active",
                    "All scheduled jobs are dead. Re-enable in Kernel.php or move to "
                    "bootstrap/app.php (Laravel 11+). Cleanup and notification jobs won't run.",
                )
            elif _sched_active > 0:
                ok(f"Scheduler: {_sched_active} active scheduled task(s)")
            elif _sched_commented == 0 and _sched_active == 0:
                warn("No scheduled tasks found — project may not use the scheduler")
        except Exception as e:
            warn(f"Scheduler check skipped: {e}")

        # 6e. Source readability: sample a method and verify source can be read back
        try:
            sample_nodes = db.execute(
                "MATCH (n:Method) WHERE n.file_path IS NOT NULL AND n.line_start IS NOT NULL "
                "RETURN n.file_path AS fp, n.line_start AS ls, n.line_end AS le LIMIT 1"
            )
            if sample_nodes:
                from laravelgraph.mcp.explain import read_source_snippet
                node = sample_nodes[0]
                fp, ls, le = node.get("fp"), node.get("ls"), node.get("le")
                le = le or ((ls or 0) + 50)
                snippet = read_source_snippet(fp, ls, le, root) if fp and ls else None
                if snippet and snippet.strip():
                    ok(f"Source readability: OK ({len(snippet.splitlines())} lines from {Path(fp).name})")
                else:
                    fail(
                        "Source readability: cannot read source for indexed methods",
                        f"File: {fp}  Lines: {ls}–{le}  Check project_root matches the indexed path",
                    )
            else:
                warn("No Method nodes with file_path — source injection unavailable")
        except Exception as e:
            fail("Source readability check failed", str(e))

    # ── 7. Transport & Server ─────────────────────────────────────────────────
    section("Transport & Server")
    import shutil as _shutil

    transport_mode = cfg.mcp.transport  # "stdio" or "http"

    # 7a. stdio transport checks
    console.print(f"  [dim]Configured transport:[/dim] {transport_mode}")

    binary = _shutil.which("laravelgraph")
    if binary:
        ok(f"laravelgraph binary found: {binary}")
    else:
        fail("laravelgraph binary not in PATH", "Run: pipx install . or pip install -e .")

    # 7b. HTTP transport checks (if configured or running)
    _http_host = cfg.mcp.host or "127.0.0.1"
    _http_port = cfg.mcp.port or 3000
    try:
        import urllib.request as _urllib
        health_url = f"http://{_http_host}:{_http_port}/health"
        req = _urllib.Request(health_url)
        with _urllib.urlopen(req, timeout=2) as resp:
            body = resp.read().decode()
            ok(f"HTTP server reachable at {health_url}")
            console.print(f"       [dim]{body[:100]}[/dim]")
    except Exception:
        if transport_mode == "http":
            fail(
                f"HTTP server not reachable at http://{_http_host}:{_http_port}/health",
                f"Start with: laravelgraph serve --http --host {_http_host} --port {_http_port}",
            )
        else:
            console.print(f"  [dim]  HTTP server not running on {_http_host}:{_http_port} (stdio mode — expected)[/dim]")

    # 7c. Show connection config for both modes
    console.print()
    console.print("  [bold]stdio config (local, auto-start):[/bold]")
    stdio_cfg = {"laravelgraph": {"type": "local", "command": ["bash", "-c", f"laravelgraph serve \"{root}\""], "enabled": True}}
    console.print(f"  [dim]{json.dumps(stdio_cfg)}[/dim]")

    console.print()
    console.print(f"  [bold]HTTP/SSE config (remote EC2):[/bold]")
    http_cfg: dict = {"laravelgraph": {"type": "sse", "url": f"http://{_http_host}:{_http_port}/sse"}}
    if cfg.mcp.api_key:
        http_cfg["laravelgraph"]["headers"] = {"Authorization": f"Bearer {cfg.mcp.api_key}"}
    console.print(f"  [dim]{json.dumps(http_cfg)}[/dim]")

    # ── 8. LLM Provider ───────────────────────────────────────────────────────
    section("LLM Provider")
    from laravelgraph.mcp.summarize import provider_status, generate_summary
    status = provider_status(cfg.llm)
    active = status["active_provider"]

    if not cfg.llm.enabled:
        warn("Summaries disabled in config")
    elif not active:
        warn("No provider configured — run: laravelgraph configure")
    else:
        ok(f"Provider: [bold]{active}[/bold]")

        # Show model
        info = status["providers"][active]
        ok(f"Model: {info['model']}")

        # Check required SDK is installed before attempting live test
        needs_openai_sdk = active in ("openai", "openrouter", "ollama")
        needs_anthropic_sdk = active == "anthropic"
        sdk_missing = False

        if needs_anthropic_sdk:
            try:
                import anthropic  # noqa
            except ImportError:
                fail("anthropic SDK not installed", "pip install anthropic")
                sdk_missing = True

        if needs_openai_sdk:
            try:
                import openai  # noqa
            except ImportError:
                fail("openai SDK not installed", "pip install openai")
                sdk_missing = True

        if not sdk_missing:
            # Live test — send a real prompt
            console.print(f"  [dim]→ Sending test prompt to {active}...[/dim]")
            t0 = time.perf_counter()
            summary, used_provider = generate_summary(
                fqn="App\\Http\\Controllers\\HealthController::check",
                node_type="controller method",
                source="public function check(): JsonResponse { return response()->json(['status' => 'ok']); }",
                summary_cfg=cfg.llm,
            )
            elapsed = time.perf_counter() - t0

            if summary:
                ok(f"Live test passed [dim]({elapsed:.2f}s)[/dim]")
                console.print(f"\n  [dim italic]\"{summary}\"[/dim italic]\n")
            else:
                fail(
                    f"Live test failed — no response from {active}",
                    "Check your model name / network connection, then run: laravelgraph configure",
                )

    # ── 9. Optional Features ──────────────────────────────────────────────────
    section("Optional Features")
    try:
        import watchfiles  # noqa
        ok("watchfiles installed — watch mode available")
    except ImportError:
        warn("watchfiles not installed — watch mode unavailable (pip install watchfiles)")

    try:
        import fastembed  # noqa
        ok("fastembed installed — vector search available")
    except ImportError:
        warn("fastembed not installed — vector search unavailable (pip install fastembed)")

    # ── 10. Database Connections ───────────────────────────────────────────────
    section("Database Connections")
    try:
        import pymysql  # noqa
        ok("PyMySQL installed — live DB introspection available")
    except ImportError:
        fail("PyMySQL not installed — live DB introspection unavailable", "pip install PyMySQL")

    db_connections = cfg.databases if hasattr(cfg, "databases") else []
    if not db_connections:
        warn("No DB connections configured — migration-based schema only")
        console.print("       [dim]Run: laravelgraph db-connections add[/dim]")
    else:
        ok(f"{len(db_connections)} connection(s) configured")
        for db_conn in db_connections:
            conn_name = getattr(db_conn, "name", "?")
            host = getattr(db_conn, "host", "?")
            dbname = getattr(db_conn, "database", "?")
            console.print(f"  [dim]  {conn_name}: {host}/{dbname}[/dim]")

            # Connectivity test
            try:
                import pymysql
                import os as _os
                password = getattr(db_conn, "password", "")
                if isinstance(password, str) and password.startswith("${") and password.endswith("}"):
                    env_var = password[2:-1]
                    password = _os.environ.get(env_var, "")
                pymysql.connect(
                    host=getattr(db_conn, "host", "127.0.0.1"),
                    port=int(getattr(db_conn, "port", 3306)),
                    database=getattr(db_conn, "database", ""),
                    user=getattr(db_conn, "username", ""),
                    password=password,
                    ssl={"ssl": {}} if getattr(db_conn, "ssl", False) else None,
                    connect_timeout=5,
                ).close()
                ok(f"  Connection `{conn_name}` — reachable")
            except Exception as e:
                fail(
                    f"  Connection `{conn_name}` — {e}",
                    "Run: laravelgraph db-connections test",
                )

    # Check indexed DB stats if a project is indexed (phases 24-26 health)
    from laravelgraph.config import index_dir as _index_dir_doctor
    if root and (_index_dir_doctor(root) / "graph.kuzu").exists():
        def _silent_count(_db, query: str) -> int:
            """Run a count query without logging errors (schema may be outdated)."""
            try:
                result = _db.execute_raw(query)
                if result and result.has_next():
                    return result.get_next()[0] or 0
            except Exception:
                pass
            return -1  # -1 = query not supported (old schema)

        try:
            from laravelgraph.core.graph import GraphDB
            _db = GraphDB(_index_dir_doctor(root) / "graph.kuzu")

            # ── Phase 19: migration-derived tables ────────────────────────────
            mig_cnt = _silent_count(
                _db,
                "MATCH (t:DatabaseTable) WHERE t.source = 'migration' OR t.connection IS NULL "
                "RETURN count(*)"
            )
            col_cnt = _silent_count(_db, "MATCH (c:DatabaseColumn) RETURN count(*)")
            if mig_cnt >= 0:
                ok(f"Phase 19 (migration schema): {mig_cnt} tables, {max(col_cnt, 0)} columns")
            else:
                warn("Phase 19 stats unavailable")

            # ── Phase 24: live DB introspection ───────────────────────────────
            live_cnt = _silent_count(
                _db,
                "MATCH (t:DatabaseTable) WHERE t.source = 'live_db' RETURN count(*)"
            )
            fk_cnt = _silent_count(
                _db,
                "MATCH (:DatabaseTable)-[:REFERENCES_TABLE {enforced: true}]->(:DatabaseTable) "
                "RETURN count(*)"
            )
            proc_cnt = _silent_count(_db, "MATCH (p:StoredProcedure) RETURN count(*)")
            if live_cnt > 0:
                ok(f"Phase 24 (live DB): {live_cnt} live tables, {max(fk_cnt, 0)} enforced FKs, {max(proc_cnt, 0)} procedures")
            elif db_connections:
                fail(
                    "Phase 24 (live DB): 0 live tables — introspection did not complete",
                    "Run: laravelgraph analyze --phases 24,25,26",
                )
            else:
                warn("Phase 24 (live DB): no connections configured — migration schema only")

            # ── Schema health: check if INT32 overflow fix is in place ────────
            try:
                # CALL table_info() returns column names + types for the node table
                _col_type_result = _db.execute_raw("CALL table_info('DatabaseColumn') RETURN *")
                _length_type = None
                while _col_type_result and _col_type_result.has_next():
                    row = _col_type_result.get_next()
                    # row is (name, type, ...) depending on KuzuDB version
                    if row and len(row) >= 2 and str(row[0]).lower() == "length":
                        _length_type = str(row[1]).upper()
                        break
                if _length_type in ("INT64", "INT32"):
                    if _length_type == "INT64":
                        ok("Schema health: DatabaseColumn.length is INT64 (overflow-safe)")
                    else:
                        fail(
                            "Schema health: DatabaseColumn.length is INT32 — LONGTEXT columns will overflow",
                            "Run: laravelgraph analyze . --full  to rebuild with the fixed schema",
                        )
                else:
                    # Couldn't determine — not a failure, just skip
                    pass
            except Exception:
                pass  # CALL table_info not supported on this KuzuDB version — skip silently

            # ── Phase 25: model-table links ───────────────────────────────────
            uses_table_cnt = _silent_count(
                _db, "MATCH (:EloquentModel)-[:USES_TABLE]->(:DatabaseTable) RETURN count(*)"
            )
            model_cnt = _silent_count(_db, "MATCH (m:EloquentModel) RETURN count(*)")
            if uses_table_cnt > 0:
                ok(f"Phase 25 (model-table links): {uses_table_cnt} USES_TABLE edges across {max(model_cnt, 0)} models")
            elif model_cnt > 0:
                warn(
                    f"Phase 25 (model-table links): 0 links for {model_cnt} models — "
                    "run: laravelgraph analyze --phases 19,24,25"
                )
            else:
                warn("Phase 25: no Eloquent models found")

            # ── Phase 26: DB access analysis ──────────────────────────────────
            qt_cnt = _silent_count(_db, "MATCH ()-[:QUERIES_TABLE]->() RETURN count(*)")
            inferred_cnt = _silent_count(_db, "MATCH (n:InferredRelationship) RETURN count(*)")
            if qt_cnt > 0:
                ok(f"Phase 26 (DB access): {qt_cnt} QUERIES_TABLE edges, {max(inferred_cnt, 0)} inferred relationships")
            else:
                warn("Phase 26 (DB access): 0 QUERIES_TABLE edges — run: laravelgraph analyze --phases 26")

        except Exception as e:
            warn(f"DB graph stats unavailable: {e}")

    # ── Query cache stats ──────────────────────────────────────────────────────
    from laravelgraph.config import index_dir as _index_dir_qc
    try:
        from laravelgraph.mcp.query_cache import QueryResultCache
        _qc = QueryResultCache(_index_dir_qc(root))
        _qc_stats = _qc.stats()
        live = _qc_stats["live_entries"]
        total = _qc_stats["cached_entries"]
        if total > 0:
            ok(f"Query cache: {live} live entries, {total - live} expired ({total} total) — use 'laravelgraph db-query --clear-cache' to reset")
        else:
            ok("Query cache: empty (populated on first db-query call)")
    except Exception:
        pass

    # ── MCP Tool Signatures ────────────────────────────────────────────────────
    # Validates that every tool accepts the parameter aliases agents commonly use.
    # Catches regressions where a parameter rename breaks agent compatibility.
    section("MCP Tool Signatures")
    try:
        import asyncio as _asyncio
        from laravelgraph.mcp.server import create_server as _create_server
        _mcp_server = _create_server(root)

        # list_tools() is async in FastMCP — fetch all registered tools once
        _tools_list: list = []
        try:
            _raw = _mcp_server.list_tools()
            if _asyncio.iscoroutine(_raw):
                _tools_list = _asyncio.run(_raw)
            else:
                _tools_list = list(_raw)
        except Exception:
            _tools_list = []

        # Build map: tool_name → set of accepted parameter names
        # FastMCP FunctionTool exposes parameters dict OR inputSchema dict depending on version
        _tool_params: dict[str, set[str]] = {}
        for _t in _tools_list:
            _name = getattr(_t, "name", None)
            if not _name:
                continue
            # Try .parameters first (FastMCP ≥0.4 FunctionTool)
            _params_raw = getattr(_t, "parameters", None)
            if isinstance(_params_raw, dict) and "properties" in _params_raw:
                _tool_params[_name] = set(_params_raw["properties"].keys())
            else:
                # Fall back to inputSchema (older FastMCP versions)
                _schema = getattr(_t, "inputSchema", None) or {}
                _props = _schema.get("properties", {}) if isinstance(_schema, dict) else {}
                _tool_params[_name] = set(_props.keys())

        # Define expected parameter scenarios for each tool.
        # Each entry: (tool_name, scenario_label, required_params)
        _scenarios: list[tuple[str, str, list[str]]] = [
            # laravelgraph_query
            ("laravelgraph_query",           "query= param",          ["query"]),
            ("laravelgraph_query",           "q= alias",              ["q"]),
            # laravelgraph_routes
            ("laravelgraph_routes",          "filter= shorthand",     ["filter"]),
            ("laravelgraph_routes",          "filter_uri= param",     ["filter_uri"]),
            ("laravelgraph_routes",          "filter_method= param",  ["filter_method"]),
            # laravelgraph_context
            ("laravelgraph_context",         "symbol= param",         ["symbol"]),
            # laravelgraph_impact
            ("laravelgraph_impact",          "symbol= param",         ["symbol"]),
            # laravelgraph_request_flow
            ("laravelgraph_request_flow",    "route= param",          ["route"]),
            # laravelgraph_models
            ("laravelgraph_models",          "model_name= param",     ["model_name"]),
            ("laravelgraph_models",          "name= alias",           ["name"]),
            ("laravelgraph_models",          "model= alias",          ["model"]),
            # laravelgraph_config_usage
            ("laravelgraph_config_usage",    "key= param",            ["key"]),
            ("laravelgraph_config_usage",    "symbol= alias",         ["symbol"]),
            # laravelgraph_db_context
            ("laravelgraph_db_context",      "table= param",          ["table"]),
            # laravelgraph_schema
            ("laravelgraph_schema",          "table_name= param",     ["table_name"]),
            # laravelgraph_explain
            ("laravelgraph_explain",         "feature= param",        ["feature"]),
            # laravelgraph_feature_context
            ("laravelgraph_feature_context", "feature= param",        ["feature"]),
            # laravelgraph_suggest_tests
            ("laravelgraph_suggest_tests",   "symbol= param",         ["symbol"]),
            # laravelgraph_cypher
            ("laravelgraph_cypher",          "query= param",          ["query"]),
            # laravelgraph_db_query
            ("laravelgraph_db_query",        "sql= param",            ["sql"]),
            # laravelgraph_db_impact
            ("laravelgraph_db_impact",       "table= param",          ["table"]),
        ]

        # Verify that ALL expected tools were actually registered
        _expected_tools = {s[0] for s in _scenarios}
        _missing_tools = _expected_tools - set(_tool_params.keys())
        if _missing_tools:
            for _mt in sorted(_missing_tools):
                fail(f"Tool not registered: {_mt}", "Check create_server() in mcp/server.py")

        _tool_failures: list[str] = []
        _tool_passes = 0
        for _tool_name, _label, _required in _scenarios:
            _found_params = _tool_params.get(_tool_name, set())
            _missing = [p for p in _required if p not in _found_params]
            if _missing:
                _tool_failures.append(
                    f"{_tool_name} ({_label}): missing params {_missing}"
                )
            else:
                _tool_passes += 1

        if _tool_failures:
            for _msg in _tool_failures:
                fail(_msg, "Add parameter alias to tool definition in mcp/server.py")
        else:
            ok(f"All {_tool_passes} tool signature scenarios pass")

    except Exception as _e:
        warn(f"Tool signature check skipped: {_e}")

    # ── 11. Plugins ───────────────────────────────────────────────────────────
    section("Plugins")
    _plugins_dir = root / ".laravelgraph" / "plugins"
    if not _plugins_dir.exists() or not list(_plugins_dir.glob("*.py")):
        warn("No plugins installed — run: laravelgraph plugin suggest")
    else:
        from laravelgraph.plugins.validator import PluginValidationError, validate_plugin
        _plugin_files = sorted(_plugins_dir.glob("*.py"))
        for _pf in _plugin_files:
            try:
                _manifest, _warnings = validate_plugin(_pf)
                if _warnings:
                    warn(f"Plugin '{_manifest['name']}' v{_manifest['version']} — {len(_warnings)} warning(s): {_warnings[0]}")
                else:
                    ok(f"Plugin '{_manifest['name']}' v{_manifest['version']} — valid")
            except PluginValidationError as _pve:
                fail(f"Plugin '{_pf.name}' — INVALID: {_pve.errors[0] if _pve.errors else str(_pve)}")

    # ── 11b. Plugin System Infrastructure ─────────────────────────────────────
    section("Plugin System")

    # 1. Core imports
    try:
        from laravelgraph.plugins.plugin_graph import init_plugin_graph, DualDB
        ok("plugin_graph module — importable")
    except Exception as _e:
        fail(f"plugin_graph module — import failed: {_e}")

    try:
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta
        ok("plugin meta module — importable")
    except Exception as _e:
        fail(f"plugin meta module — import failed: {_e}")

    try:
        from laravelgraph.plugins.generator import generate_plugin, _validate_schema, _validate_execution, ValidationResult
        ok("plugin generator module — importable")
    except Exception as _e:
        fail(f"plugin generator module — import failed: {_e}")

    try:
        from laravelgraph.plugins.self_improve import check_and_improve, run_improvement_check_all
        ok("plugin self_improve module — importable")
    except Exception as _e:
        fail(f"plugin self_improve module — import failed: {_e}")

    try:
        from laravelgraph.logging_manager import LogManager
        ok("logging_manager module — importable")
    except Exception as _e:
        fail(f"logging_manager module — import failed: {_e}")

    # 2. Plugin graph init (smoke test with temp dir)
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as _tmpdir:
            from pathlib import Path as _Path
            _pdb = init_plugin_graph(_Path(_tmpdir))
            _pdb.execute("MATCH (n:PluginNode) RETURN n.node_id LIMIT 1")
            _pdb.close()
        ok("plugin_graph init + schema + query — working")
    except Exception as _e:
        fail(f"plugin_graph smoke test — {_e}")

    # 3. PluginMetaStore smoke test
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as _tmpdir:
            from pathlib import Path as _Path
            _ms = PluginMetaStore(_Path(_tmpdir))
            _ms.set(PluginMeta(name="doctor-test", status="active"))
            _ms.log_call("doctor-test", empty=False, error=False)
            _retrieved = _ms.get("doctor-test")
            assert _retrieved is not None and _retrieved.call_count == 1
            _ms.delete("doctor-test")
            assert _ms.get("doctor-test") is None
        ok("PluginMetaStore — set / log_call / get / delete working")
    except Exception as _e:
        fail(f"PluginMetaStore smoke test — {_e}")

    # 4. DualDB backwards-compat smoke test
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as _tmpdir:
            from pathlib import Path as _Path
            _pdb2 = init_plugin_graph(_Path(_tmpdir))
            _calls = []
            def _mock_core(): _calls.append(1); return _pdb2
            _dual = DualDB(_mock_core, _pdb2)
            assert _dual() is _dual, "DualDB() should return self"
            assert _dual.plugin() is _pdb2, "DualDB.plugin() should return plugin db"
            _pdb2.close()
        ok("DualDB — callable + .plugin() + backwards-compat working")
    except Exception as _e:
        fail(f"DualDB smoke test — {_e}")

    # 5. Validation layer smoke tests
    try:
        from laravelgraph.plugins.generator import _validate_schema, _validate_execution
        _good_code = '''
PLUGIN_MANIFEST = {"name": "doctor-test", "version": "1.0.0", "description": "test", "tool_prefix": "doctest_"}
def register_tools(mcp, db=None):
    @mcp.tool()
    def doctest_query() -> str:
        return "result"
'''
        _l2 = _validate_schema(_good_code)
        assert _l2.passed, f"Schema validation failed: {_l2.critique}"
        _l3 = _validate_execution(_good_code, None)
        assert _l3.passed, f"Execution validation failed: {_l3.critique}"
        ok("Validation layers 2 (schema) + 3 (execution) — working")
    except Exception as _e:
        fail(f"Validation layer smoke test — {_e}")

    # 6. LogManager smoke test
    try:
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as _tmpdir:
            from pathlib import Path as _Path
            _lp = _Path(_tmpdir) / "test.log"
            _lp.write_text(_json.dumps({"level": "info", "event": "doctor test", "tool": "laravelgraph_routes"}) + "\n")
            _lm = LogManager(_Path(_tmpdir))
            _entries = _lm.get_recent(limit=10)
            assert len(_entries) == 1 and _entries[0]["level"] == "info"
            _stats = _lm.get_stats()
            assert _stats["total_entries"] == 1
        ok("LogManager — read / filter / stats working")
    except Exception as _e:
        fail(f"LogManager smoke test — {_e}")

    # 7. Check new MCP tools are registered
    try:
        from laravelgraph.mcp.server import create_server
        import inspect
        _src = inspect.getsource(create_server)
        _required_tools = ["laravelgraph_request_plugin", "laravelgraph_update_plugin", "laravelgraph_remove_plugin"]
        _missing = [t for t in _required_tools if t not in _src]
        if _missing:
            fail(f"MCP auto-generation tools missing: {', '.join(_missing)}")
        else:
            ok(f"MCP auto-generation tools registered — request_plugin, update_plugin, remove_plugin")
    except Exception as _e:
        fail(f"MCP tool check — {_e}")

    # 8. Improvement threshold logic
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as _tmpdir:
            from pathlib import Path as _Path
            _ms2 = PluginMetaStore(_Path(_tmpdir))
            _ms2.set(PluginMeta(name="underperformer", call_count=25, empty_result_count=8, status="active"))
            assert _ms2.check_improvement_needed("underperformer") is True
            _ms2.set(PluginMeta(name="healthy", call_count=25, empty_result_count=1, status="active"))
            assert _ms2.check_improvement_needed("healthy") is False
        ok("Self-improvement threshold detection — working (25 calls / 8 empty → triggers)")
    except Exception as _e:
        fail(f"Improvement threshold smoke test — {_e}")

    # ── 11c. Plugin Generator live test ───────────────────────────────────────
    section("Plugin Generator")
    try:
        from laravelgraph.plugins.generator import generate_plugin as _gen_plugin
        from laravelgraph.mcp.summarize import _resolve_provider  # type: ignore[attr-defined]

        _llm_available = False
        try:
            _llm_available = bool(_resolve_provider(cfg.llm))
        except Exception:
            pass

        if not _llm_available:
            warn("Plugin Generator live test skipped — no LLM provider configured (run: laravelgraph providers add)")
        else:
            import time as _time
            _test_desc = "List all HTTP routes with their methods and controller actions"
            _t0 = _time.time()
            try:
                class _MockPluginDB:
                    def execute(self, q, p=None):
                        if "Route" in q:
                            return [
                                {"m": "GET", "u": "/api/health", "a": "HealthController@index"},
                                {"m": "POST", "u": "/api/users", "a": "UserController@store"},
                            ]
                        if "EloquentModel" in q or "Event" in q or "Feature" in q or "DatabaseTable" in q:
                            return []
                        return []
                    def plugin(self):
                        return self
                    def upsert_plugin_node(self, *a, **kw):
                        pass

                _code, _msg = _gen_plugin(
                    description=_test_desc,
                    project_root=root,
                    core_db=_MockPluginDB(),
                    cfg=cfg,
                    max_iterations=2,
                )
                _elapsed = round(_time.time() - _t0, 1)
                if _code is not None:
                    ok(f"Plugin Generator — generated in {_elapsed}s: {_msg[:80]}")
                else:
                    fail(f"Plugin Generator — generation failed ({_elapsed}s)", _msg[:120])
            except Exception as _gen_err:
                _elapsed = round(_time.time() - _t0, 1)
                fail(f"Plugin Generator — error ({_elapsed}s): {_gen_err}")
    except ImportError as _ie:
        warn(f"Plugin Generator test skipped — import error: {_ie}")

    # ── 12. Downloads ─────────────────────────────────────────────────────────
    section("Downloads")
    try:
        from laravelgraph.downloads import check_all as _check_downloads
        _dl_statuses = _check_downloads()
        for _dl_key, _dl_ready in _dl_statuses.items():
            if _dl_ready:
                ok(f"{_dl_key} — ready")
            else:
                warn(f"{_dl_key} — not downloaded (run: laravelgraph download)")
    except Exception as _dl_err:
        warn(f"Downloads check skipped: {_dl_err}")

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    if failures == 0:
        console.print(Panel(
            f"[green]All checks passed[/green]  ({passes} passed, 0 failed)",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[red]{failures} check(s) failed[/red]  ({passes} passed, {failures} failed)\n"
            "Fix the issues above and run [bold]laravelgraph doctor[/bold] again.",
            border_style="red",
        ))
        raise typer.Exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_indexed(root: Path) -> None:
    from laravelgraph.core.registry import Registry
    if not Registry().is_indexed(root):
        console.print(f"[red]Not indexed:[/red] {root}")
        console.print("Run [bold]laravelgraph analyze[/bold] first.")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
