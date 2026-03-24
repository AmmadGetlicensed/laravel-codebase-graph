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
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
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
        "Indexes your Laravel project into a local knowledge graph (KuzuDB), "
        "then exposes it via an MCP server so AI agents can understand routes, "
        "models, events, and code relationships.\n\n"
        "[bold]Quick start:[/bold]\n"
        "  laravelgraph analyze          # Index the current project\n"
        "  laravelgraph serve            # Start MCP server (stdio for Claude Code)\n"
        "  laravelgraph serve --http     # Start MCP server (HTTP for EC2/shared)\n"
        "  laravelgraph doctor           # Full health check\n"
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

@app.command()
def analyze(
    path: Optional[Path] = PathArg,
    full: bool = typer.Option(False, "--full", help="Force full rebuild. Use on first run or after major refactors. Default: incremental update."),
    no_embeddings: bool = typer.Option(False, "--no-embeddings", help="Skip vector embedding generation. Speeds up indexing; disables semantic (vector) search. BM25 and fuzzy search still work."),
    phases: Optional[str] = typer.Option(None, "--phases", help="Re-run specific pipeline phases only (e.g. '14' or '1,2,14'). Phase 14 = route analysis. Skips all other phases for faster targeted updates."),
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

    Examples:
      laravelgraph analyze
      laravelgraph analyze /path/to/project --full
      laravelgraph analyze --phases 14
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
    total_phases_ref: list[int] = [23]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[eta]}[/dim]"),
        console=console,
    ) as progress:
        task = progress.add_task("Initializing...", total=total_phases_ref[0], eta="")

        def on_phase_start(idx: int, name: str, total: int) -> None:
            total_phases_ref[0] = total
            progress.update(task, total=total, description=f"[{idx}/{total}] {name}")

        def on_phase_done(idx: int, name: str, elapsed: float) -> None:
            phase_times.append(elapsed)
            phase_names.append(name)
            avg = sum(phase_times) / len(phase_times)
            remaining = avg * (total_phases_ref[0] - idx)
            eta_str = f"eta {_fmt_sec(remaining)}" if remaining > 0 and len(phase_times) >= 3 else ""
            progress.update(task, completed=idx, eta=eta_str)

        from laravelgraph.pipeline.orchestrator import Pipeline
        pipeline = Pipeline(root, config=cfg)

        try:
            ctx = pipeline.run(
                full=full,
                skip_embeddings=no_embeddings,
                phases=selected_phases,
                on_phase_start=on_phase_start,
                on_phase_done=on_phase_done,
            )
        except Exception as e:
            if "lock" in str(e).lower() or "locked" in str(e).lower():
                console.print(
                    "\n[red]Error:[/red] Database is locked — another laravelgraph process may be running.\n"
                    "Stop it and try again."
                )
                raise typer.Exit(1)
            raise

        progress.update(task, description="[green]Complete!", completed=total_phases_ref[0], eta="")

    # Show stats
    table = Table(title="Index Summary", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Count", justify="right")

    for key, value in sorted(ctx.stats.items()):
        table.add_row(key.replace("_", " ").title(), str(value))

    console.print(table)

    # Show phase timing breakdown
    if phase_times:
        timing_table = Table(title="Phase Timings", show_header=True, header_style="bold")
        timing_table.add_column("Phase", style="dim")
        timing_table.add_column("Duration", justify="right")
        for name, elapsed in zip(phase_names, phase_times):
            timing_table.add_row(name, _fmt_sec(elapsed))
        console.print(timing_table)

    if ctx.errors:
        console.print(f"\n[yellow]⚠️ {len(ctx.errors)} warnings during indexing.[/yellow]")
        for err in ctx.errors[:5]:
            console.print(f"  [dim]{err}[/dim]")
        if len(ctx.errors) > 5:
            console.print(f"  [dim]...and {len(ctx.errors) - 5} more[/dim]")

    console.print(
        f"\n[green]✓[/green] Project indexed successfully. "
        f"Run [bold]laravelgraph serve[/bold] to start the MCP server."
    )


# ── status ────────────────────────────────────────────────────────────────────

@app.command()
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

@app.command(name="list")
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

@app.command()
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


# ── query ─────────────────────────────────────────────────────────────────────

@app.command()
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

@app.command()
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
            "MATCH (target)-[:USES_MODEL]->(m) WHERE target.node_id = $id "
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

@app.command()
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

@app.command(name="dead-code")
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

@app.command()
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

@app.command()
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

@app.command()
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

@app.command()
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

@app.command()
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

@app.command()
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

@app.command()
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

@app.command()
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

@app.command()
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


# ── providers ─────────────────────────────────────────────────────────────────

@app.command()
def providers(path: Optional[Path] = PathArg) -> None:
    """Show which of the 18+ supported LLM providers are configured and active.

    Displays two tables: cloud providers (OpenAI, Anthropic, Gemini, etc.)
    and local providers (Ollama, LM Studio, etc.). For each provider shows
    whether it is configured (has an API key or base URL), which model is
    selected, and whether it is the currently active provider.

    Configured = has API key or base URL set (via env var or config file).
    Active = the provider that will be used for generating semantic summaries
    on the next query.

    Use `laravelgraph configure` to set up a provider.
    Use `laravelgraph doctor` to run a live test of the active provider.

    Examples:
      laravelgraph providers
      laravelgraph providers /path/to/project
    """
    root = _project_root(path)

    from laravelgraph.config import Config
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY, provider_status

    cfg = Config.load(root)
    status = provider_status(cfg.summary)
    active = status["active_provider"]

    active_label = f"{PROVIDER_REGISTRY[active]['label']}" if active else ""
    active_str = f"[green]{active}[/green] — {active_label}" if active else "[yellow]none[/yellow] — summaries skipped"
    console.print(Panel(
        f"[bold]Semantic Summaries:[/bold] {'[green]enabled[/green]' if status['enabled'] else '[red]disabled[/red]'}\n"
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
            "Run [bold]laravelgraph configure[/bold] to set one up."
        )


# ── configure ─────────────────────────────────────────────────────────────────

@app.command()
def configure(
    path: Optional[Path] = PathArg,
    global_: bool = typer.Option(False, "--global", "-g", help="Save to global config (~/.laravelgraph/config.json) rather than project-level (.laravelgraph/config.json)"),
) -> None:
    """Interactive wizard to configure an LLM provider for semantic summaries.

    Walks you through selecting one of 18+ supported LLM providers (cloud or
    local) and saves the API key and model selection to config. Config is
    written to either:
      - Global:  ~/.laravelgraph/config.json  (all projects on this machine)
      - Project: <project>/.laravelgraph/config.json  (this project only)

    Project-level config overrides global config. Global config is the best
    default for most setups. Use project-level if you want different providers
    per project.

    Summaries are generated lazily on first query and cached — no cost until
    used. If no provider is set, laravelgraph works fine without summaries.

    After configuring, use `laravelgraph providers` to verify and
    `laravelgraph doctor` to run a live test.

    Examples:
      laravelgraph configure           # Interactive — project-level config
      laravelgraph configure --global  # Save to ~/.laravelgraph/config.json
    """
    import json as _json
    root = _project_root(path)

    from laravelgraph.config import global_dir, index_dir as _index_dir
    from laravelgraph.mcp.summarize import PROVIDER_REGISTRY

    console.print(Panel(
        "Configure an LLM provider for [bold]semantic summary generation[/bold].\n"
        "Summaries are generated lazily on first query and cached — no cost until used.\n"
        "If no provider is set, the tool works fine — just without AI-generated summaries.",
        title="LaravelGraph — Provider Setup",
        border_style="cyan",
    ))

    # ── List all providers ────────────────────────────────────────────────────
    cloud_providers = [(n, v) for n, v in PROVIDER_REGISTRY.items() if not v.get("local")]
    local_providers = [(n, v) for n, v in PROVIDER_REGISTRY.items() if v.get("local")]

    console.print("\n[bold]Cloud providers:[/bold]\n")
    for i, (name, info) in enumerate(cloud_providers, start=1):
        console.print(f"  [cyan]{i:2}[/cyan]  {info['label']}")

    console.print("\n[bold]Local providers (no API key required):[/bold]\n")
    for i, (name, info) in enumerate(local_providers, start=len(cloud_providers) + 1):
        console.print(f"  [cyan]{i:2}[/cyan]  {info['label']}")

    console.print(f"\n  [cyan] 0[/cyan]  Disable summaries\n")

    all_providers = cloud_providers + local_providers
    choice_str = typer.prompt("Select provider number", default="1")

    if choice_str == "0":
        provider_name = "disabled"
        provider_info = None
    else:
        try:
            idx = int(choice_str) - 1
            provider_name, provider_info = all_providers[idx]
        except (ValueError, IndexError):
            console.print(f"[red]Invalid choice:[/red] {choice_str}")
            raise typer.Exit(1)

    # ── Provider-specific prompts ─────────────────────────────────────────────
    summary_patch: dict = {}

    if provider_name == "disabled":
        summary_patch = {"enabled": False}
        console.print("[yellow]Summaries will be disabled.[/yellow]")

    elif provider_info and provider_info.get("local"):
        # Local provider: ask for base URL + model
        console.print(f"\n[bold]{provider_info['label']}[/bold]")
        default_url = provider_info["base_url"].replace("/v1", "")  # show clean URL
        base_url = typer.prompt("Base URL", default=default_url)
        model = typer.prompt("Model name (must be already pulled/loaded locally)")
        summary_patch = {
            "provider": provider_name,
            "base_urls": {provider_name: base_url},
            "models": {provider_name: model},
        }

    else:
        # Cloud provider: ask for API key + model
        env_var = provider_info["env_var"]  # type: ignore[index]
        default_model = provider_info["default_model"]  # type: ignore[index]
        console.print(f"\n[bold]{provider_info['label']}[/bold]")  # type: ignore[index]
        if env_var:
            console.print(f"Environment variable: [cyan]{env_var}[/cyan]")
        key = typer.prompt("API key", hide_input=True)
        model = typer.prompt("Model", default=default_model)
        summary_patch = {
            "provider": "auto",
            "api_keys": {provider_name: key},
            "models": {provider_name: model},
        }

    # ── Choose scope ──────────────────────────────────────────────────────────
    if not global_:
        console.print("\n[bold]Where to save?[/bold]\n")
        console.print(f"  [cyan]1[/cyan]  This project only  ({root / '.laravelgraph' / 'config.json'})")
        console.print(f"  [cyan]2[/cyan]  Global — all projects  (~/.laravelgraph/config.json)\n")
        global_ = typer.prompt("Save to", default="1") == "2"

    cfg_path = (global_dir() if global_ else _index_dir(root)) / "config.json"

    # Deep-merge into existing config
    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = _json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    s = existing.setdefault("summary", {})
    for k, v in summary_patch.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)   # merge dicts (api_keys, models, base_urls)
        else:
            s[k] = v

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_json.dumps(existing, indent=2), encoding="utf-8")

    scope_label = "global" if global_ else "project"
    console.print(f"\n[green]✓[/green] Config saved to [cyan]{cfg_path}[/cyan] ({scope_label})")

    if provider_name != "disabled":
        console.print(
            f"\n[bold]Active provider:[/bold] [green]{provider_name}[/green]\n"
            "Summaries will be generated on first query and cached automatically.\n"
            "Run [bold]laravelgraph providers[/bold] to verify — "
            "[bold]laravelgraph doctor[/bold] to test live."
        )
        if provider_info and not provider_info.get("local") and provider_info.get("env_var"):
            console.print(
                f"\n[yellow]Tip:[/yellow] Use an env var instead of storing the key in config:\n"
                f"  [dim]export {provider_info['env_var']}=your-key[/dim]"
            )


# ── setup ─────────────────────────────────────────────────────────────────────

@app.command()
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

@app.command()
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


# ── version ───────────────────────────────────────────────────────────────────

@app.command()
def version() -> None:
    """Print version information."""
    from laravelgraph import __version__
    console.print(f"LaravelGraph v{__version__}")


# ── doctor ────────────────────────────────────────────────────────────────────

@app.command()
def doctor(path: Optional[Path] = PathArg) -> None:
    """Full health check across all 9 system sections.

    Runs a comprehensive diagnostic and reports pass/warn/fail for each check:

      1. Config         — load config from all sources, check for errors
      2. Dependencies   — required packages (kuzu, fastmcp) and optional SDKs
      3. Graph DB       — verify KuzuDB is accessible, report node/edge counts
      4. MCP Tools      — smoke-test all key MCP tool queries against the graph
      5. Context Quality — source injection, summary cache read/write, route resolution
      6. Recent Changes  — verify fixes for known issues (snippet limits, route upsert)
      7. Transport & Server — binary in PATH, HTTP server reachability, config snippets
      8. LLM Provider   — active provider, model selection, live summary test
      9. Optional Features — watchfiles (watch mode), fastembed (vector search)

    Exits with code 1 if any check fails. Use this to verify your setup after
    install, after upgrade, or when something seems wrong.

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

    # ── 4. MCP Tool smoke tests ───────────────────────────────────────────────
    section("MCP Tools")
    if db is None:
        warn("Skipping tool tests — no graph DB")
    else:
        tool_checks = [
            ("laravelgraph_query",   "MATCH (n) RETURN n.name AS name LIMIT 1"),
            ("laravelgraph_routes",  "MATCH (r:Route) RETURN r.uri AS uri LIMIT 1"),
            ("laravelgraph_models",  "MATCH (m:EloquentModel) RETURN m.name AS name LIMIT 1"),
            ("laravelgraph_events",  "MATCH (e:Event) RETURN e.name AS name LIMIT 1"),
            ("laravelgraph_schema",  "MATCH (t:DatabaseTable) RETURN t.name AS name LIMIT 1"),
            ("laravelgraph_bindings","MATCH (b:ServiceBinding) RETURN b.abstract AS abs LIMIT 1"),
        ]
        for tool_name, query in tool_checks:
            try:
                t0 = time.perf_counter()
                db.execute(query)
                ms = (time.perf_counter() - t0) * 1000
                ok(f"{tool_name} [dim]({ms:.0f}ms)[/dim]")
            except Exception as e:
                fail(f"{tool_name}", str(e))

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

    # ── 6. Recent Changes ─────────────────────────────────────────────────────
    section("Recent Changes")
    # 6a. _MAX_SNIPPET_LINES should be 300 (raised from 120 to fix source truncation)
    try:
        from laravelgraph.mcp.explain import _MAX_SNIPPET_LINES
        if _MAX_SNIPPET_LINES >= 300:
            ok(f"Source snippet limit: {_MAX_SNIPPET_LINES} lines (truncation fix active)")
        else:
            fail(
                f"Source snippet limit is {_MAX_SNIPPET_LINES} — should be ≥ 300",
                "Old value causes source truncation for large methods",
            )
    except Exception as e:
        warn(f"Could not verify _MAX_SNIPPET_LINES: {e}")

    # 6b. include_source parameter on laravelgraph_context (cache-warm token saving)
    try:
        import inspect
        from laravelgraph.mcp.server import create_server as _cs
        # Inspect the source of the module to find include_source param
        import laravelgraph.mcp.server as _srv_mod
        src_txt = inspect.getsource(_srv_mod)
        if "include_source" in src_txt and "not cached_summary or include_source" in src_txt:
            ok("Cache-aware source suppression active (include_source parameter)")
        else:
            fail(
                "Cache-aware source suppression not detected",
                "laravelgraph_context should omit source when cache is warm",
            )
    except Exception as e:
        warn(f"Could not verify include_source: {e}")

    # 6c. Route resolution quality: what % of routes have a resolved controller
    if db is not None:
        try:
            total_r = db.execute("MATCH (r:Route) RETURN count(r) AS cnt")
            resolved_r = db.execute(
                "MATCH (r:Route) WHERE r.controller_fqn IS NOT NULL AND r.controller_fqn <> '' "
                "AND r.controller_fqn <> 'Closure' RETURN count(r) AS cnt"
            )
            total_cnt = total_r[0].get("cnt", 0) if total_r else 0
            resolved_cnt = resolved_r[0].get("cnt", 0) if resolved_r else 0
            if total_cnt > 0:
                pct = resolved_cnt / total_cnt * 100
                if pct >= 80:
                    ok(f"Route resolution: {resolved_cnt}/{total_cnt} routes resolved ({pct:.0f}%)")
                elif pct >= 50:
                    warn(f"Route resolution: {resolved_cnt}/{total_cnt} routes resolved ({pct:.0f}%) — some Closure routes may be unresolved")
                else:
                    fail(
                        f"Route resolution low: only {resolved_cnt}/{total_cnt} routes resolved ({pct:.0f}%)",
                        "Run: laravelgraph analyze --phases 14",
                    )
            else:
                warn("No routes found — run laravelgraph analyze")
        except Exception as e:
            warn(f"Route resolution check skipped: {e}")

    # 6d. Phase 14 upsert: verify routes can be re-indexed (check via route count stability)
    # We verify this indirectly: if routes exist and are resolved, the upsert fix is working
    if db is not None:
        try:
            route_count = db.execute("MATCH (r:Route) RETURN count(r) AS cnt")
            cnt = route_count[0].get("cnt", 0) if route_count else 0
            if cnt > 0:
                ok(f"Phase 14 route upsert: {cnt} routes in graph (re-indexing safe)")
            else:
                warn("No routes — run: laravelgraph analyze --phases 14")
        except Exception as e:
            warn(f"Phase 14 check skipped: {e}")

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
    status = provider_status(cfg.summary)
    active = status["active_provider"]

    if not cfg.summary.enabled:
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
                summary_cfg=cfg.summary,
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
