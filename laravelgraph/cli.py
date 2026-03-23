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
    help="Graph-powered code intelligence for Laravel codebases",
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
    full: bool = typer.Option(False, "--full", help="Force full rebuild (skip incremental)"),
    no_embeddings: bool = typer.Option(False, "--no-embeddings", help="Skip vector embedding generation"),
    phases: Optional[str] = typer.Option(None, "--phases", help="Comma-separated phase numbers to run (e.g. 1,2,3)"),
) -> None:
    """Index a Laravel project — builds the knowledge graph."""
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
    """Show index status for the current project."""
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
    """List all indexed repositories."""
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
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Delete the index for the current project."""
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
    query_str: str = typer.Argument(..., help="Search query"),
    path: Optional[Path] = ProjectOpt,
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
    role: str = typer.Option("", "--role", "-r", help="Filter by Laravel role"),
) -> None:
    """Hybrid search across all indexed Laravel symbols."""
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
    symbol: str = typer.Argument(..., help="Symbol FQN, name, or node_id"),
    path: Optional[Path] = ProjectOpt,
) -> None:
    """360° view of a symbol: callers, callees, relationships, community."""
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
            {"id": node.get("node_id", "")},
        )
        if callers:
            tree = Tree("[bold]Callers[/bold]")
            for c in callers:
                tree.add(f"[green]{c.get('fqn', '?')}[/green] (conf: {c.get('conf', '?')})")
            console.print(tree)
    except Exception:
        pass


# ── impact ────────────────────────────────────────────────────────────────────

@app.command()
def impact(
    symbol: str = typer.Argument(..., help="Symbol to analyze"),
    path: Optional[Path] = ProjectOpt,
    depth: int = typer.Option(3, "--depth", "-d", help="BFS depth (default 3)"),
) -> None:
    """Blast radius analysis — all symbols affected by changing this one."""
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
    role: str = typer.Option("", "--role", "-r", help="Filter by Laravel role"),
) -> None:
    """Dead code report — unreachable symbols with Laravel-aware exemptions."""
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
    method: str = typer.Option("", "--method", "-m", help="Filter by HTTP method"),
    uri: str = typer.Option("", "--uri", "-u", help="Filter by URI fragment"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """Route intelligence table — all routes with middleware and controllers."""
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
    model_name: str = typer.Option("", "--model", "-m", help="Filter to a specific model"),
) -> None:
    """Eloquent model relationship map."""
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
    """Event → listener → job dispatch map."""
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
    """Service container binding map."""
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
    table_filter: str = typer.Option("", "--table", "-t", help="Filter to a specific table"),
) -> None:
    """Database schema from migrations."""
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
    query_str: str = typer.Argument(..., help="Cypher query (read-only)"),
    path: Optional[Path] = ProjectOpt,
) -> None:
    """Execute a read-only Cypher query against the knowledge graph."""
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
    port: int = typer.Option(3000, "--port", help="HTTP port (when --http)"),
    host: str = typer.Option("127.0.0.1", "--host", help="HTTP host (when --http)"),
) -> None:
    """Start the MCP server for AI agent integration."""
    root = _project_root(path)

    from laravelgraph.config import Config
    from laravelgraph.logging import configure

    cfg = Config.load(root)
    configure(cfg.log.level, cfg.log.dir)

    if not http:
        # stdio transport — start silently (output breaks MCP protocol)
        from laravelgraph.mcp.server import run_stdio
        run_stdio(root, cfg)
    else:
        console.print(f"[bold green]LaravelGraph MCP Server[/bold green]")
        console.print(f"Transport: HTTP/SSE")
        console.print(f"Listening: http://{host}:{port}")
        console.print(f"Project: {root}")
        console.print("\nPress Ctrl+C to stop.")

        if watch:
            # Start file watcher in background thread
            import threading
            from laravelgraph.watch.watcher import start_watch
            watcher_thread = threading.Thread(
                target=start_watch, args=(root, cfg), daemon=True
            )
            watcher_thread.start()

        from laravelgraph.mcp.server import run_http
        run_http(root, host=host, port=port, config=cfg)


# ── watch ─────────────────────────────────────────────────────────────────────

@app.command()
def watch(path: Optional[Path] = PathArg) -> None:
    """Watch mode — live re-indexing on file changes."""
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
    revision: str = typer.Argument(..., help="Git revision range (e.g. main..feature or HEAD~3)"),
    path: Optional[Path] = ProjectOpt,
) -> None:
    """Structural branch comparison — symbols added, modified, removed."""
    root = _project_root(path)
    _ensure_indexed(root)

    console.print(f"[bold]Branch diff:[/bold] {revision}")

    try:
        from git import Repo
        repo = Repo(str(root))

        if ".." in revision:
            base, head = revision.split("..", 1)
        else:
            base = "HEAD~1"
            head = revision

        diff_obj = repo.commit(base).diff(repo.commit(head))
        changed = [(d.a_path, d.change_type) for d in diff_obj]

        table = Table(title=f"Changed files: {base}..{head}", header_style="bold")
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
    """Show all LLM providers — which are configured, active, or available."""
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
    global_: bool = typer.Option(False, "--global", "-g", help="Save to global config (~/.laravelgraph/config.json)"),
) -> None:
    """Interactive wizard to configure an LLM provider for semantic summaries."""
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
) -> None:
    """Print MCP configuration JSON for AI agents."""
    root = _project_root(path)

    config = {
        "mcpServers": {
            "laravelgraph": {
                "command": "laravelgraph",
                "args": ["serve", str(root)],
                "description": f"LaravelGraph — code intelligence for {root.name}",
            }
        }
    }

    if claude:
        console.print("\n[bold]Claude Code (~/.claude.json or .claude.json):[/bold]")
        console.print(json.dumps(config, indent=2))
    elif cursor:
        console.print("\n[bold]Cursor (~/.cursor/mcp.json):[/bold]")
        console.print(json.dumps(config, indent=2))
    elif windsurf:
        console.print("\n[bold]Windsurf (~/.windsurf/mcp_config.json):[/bold]")
        console.print(json.dumps(config, indent=2))
    else:
        console.print("\n[bold]MCP Server Configuration:[/bold]")
        console.print(json.dumps(config, indent=2))
        console.print("\nUse --claude, --cursor, or --windsurf for IDE-specific format.")


# ── export ────────────────────────────────────────────────────────────────────

@app.command()
def export(
    path: Optional[Path] = PathArg,
    fmt: str = typer.Option("json", "--format", "-f", help="Output format: json|dot|graphml"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file (default: stdout)"),
) -> None:
    """Export the graph in various formats."""
    root = _project_root(path)
    _ensure_indexed(root)

    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")
    stats = db.stats()

    if fmt == "json":
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
    else:
        console.print(f"[yellow]Format '{fmt}' not yet implemented.[/yellow]")


# ── version ───────────────────────────────────────────────────────────────────

@app.command()
def version() -> None:
    """Print version information."""
    from laravelgraph import __version__
    console.print(f"LaravelGraph v{__version__}")


# ── doctor ────────────────────────────────────────────────────────────────────

@app.command()
def doctor(path: Optional[Path] = PathArg) -> None:
    """Full health check — config, graph DB, LLM provider, and MCP tools."""
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
        fail("Graph DB error", str(e))

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

    # ── 6. LLM Provider ───────────────────────────────────────────────────────
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

    # ── 7. Optional Features ──────────────────────────────────────────────────
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
