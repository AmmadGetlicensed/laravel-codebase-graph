"""
KuzuDB graph schema for LaravelGraph.

Node types and relationship types with their properties.
All DDL is idempotent (CREATE IF NOT EXISTS semantics via KUZU).
"""

from __future__ import annotations

# ── Node type definitions ─────────────────────────────────────────────────────
# Format: (label, [(property_name, kuzu_type), ...])
# First property is always the primary key (node_id).

NODE_TYPES: list[tuple[str, list[tuple[str, str]]]] = [
    # ── Filesystem ──────────────────────────────────────────────────────────
    ("Folder", [
        ("node_id", "STRING"),
        ("path", "STRING"),
        ("name", "STRING"),
        ("relative_path", "STRING"),
    ]),
    ("File", [
        ("node_id", "STRING"),
        ("path", "STRING"),
        ("relative_path", "STRING"),
        ("name", "STRING"),
        ("extension", "STRING"),
        ("size_bytes", "INT64"),
        ("laravel_role", "STRING"),  # model|controller|middleware|job|event|listener|...
        ("php_namespace", "STRING"),
        ("lines", "INT64"),
    ]),

    # ── PHP Symbols ─────────────────────────────────────────────────────────
    ("Namespace", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),  # fully-qualified name
    ]),
    ("Class_", [  # trailing underscore avoids Kuzu keyword conflict
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
        ("line_end", "INT32"),
        ("is_abstract", "BOOLEAN"),
        ("is_final", "BOOLEAN"),
        ("laravel_role", "STRING"),
        ("is_dead_code", "BOOLEAN"),
        ("community_id", "INT32"),
        ("embedding", "FLOAT[]"),
    ]),
    ("Trait_", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
        ("is_dead_code", "BOOLEAN"),
    ]),
    ("Interface_", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
    ]),
    ("Enum_", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
        ("backed_type", "STRING"),  # "string"|"int"|""
    ]),
    ("Method", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
        ("line_end", "INT32"),
        ("visibility", "STRING"),  # public|protected|private
        ("is_static", "BOOLEAN"),
        ("is_abstract", "BOOLEAN"),
        ("return_type", "STRING"),
        ("param_types", "STRING"),   # JSON-encoded list
        ("docblock", "STRING"),
        ("is_dead_code", "BOOLEAN"),
        ("laravel_role", "STRING"),  # handle|boot|register|accessor|mutator|scope|...
        ("community_id", "INT32"),
        ("embedding", "FLOAT[]"),
    ]),
    ("Function_", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
        ("return_type", "STRING"),
        ("is_dead_code", "BOOLEAN"),
        ("embedding", "FLOAT[]"),
    ]),
    ("Property", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
        ("visibility", "STRING"),
        ("type_hint", "STRING"),
        ("is_static", "BOOLEAN"),
        ("default_value", "STRING"),
    ]),
    ("Constant", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("line_start", "INT32"),
        ("value", "STRING"),
    ]),

    # ── Laravel Constructs ──────────────────────────────────────────────────
    ("EloquentModel", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("db_table", "STRING"),     # renamed from 'table' — reserved word in KuzuDB
        ("fillable", "STRING"),     # JSON list
        ("guarded", "STRING"),      # JSON list
        ("casts", "STRING"),        # JSON object
        ("eager_loads", "STRING"),   # JSON list (eager loads) — 'with' is reserved in KuzuDB
        ("soft_deletes", "BOOLEAN"),
        ("timestamps", "BOOLEAN"),
    ]),
    ("Controller", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("controller_type", "STRING"),  # resource|invokable|api|plain
    ]),
    ("Middleware", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("alias", "STRING"),
        ("middleware_group", "STRING"),
    ]),
    ("ServiceProvider", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("deferred", "BOOLEAN"),
        ("provides", "STRING"),  # JSON list of FQNs provided
    ]),
    ("Job", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("queue", "STRING"),
        ("connection", "STRING"),
        ("tries", "INT32"),
        ("timeout", "INT32"),
        ("is_queued", "BOOLEAN"),
    ]),
    ("Event", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("broadcastable", "BOOLEAN"),
        ("broadcast_channel", "STRING"),
    ]),
    ("Listener", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("is_queued", "BOOLEAN"),
        ("queue", "STRING"),
    ]),
    ("Policy", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("model_fqn", "STRING"),
    ]),
    ("FormRequest", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("rules_summary", "STRING"),  # JSON-encoded rules keys
    ]),
    ("Resource", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("is_collection", "BOOLEAN"),
    ]),
    ("Notification", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("channels", "STRING"),  # JSON list: mail|slack|database|...
    ]),
    ("Observer", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("model_fqn", "STRING"),
    ]),
    ("Command", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("signature", "STRING"),
        ("description", "STRING"),
    ]),
    ("Factory", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("model_fqn", "STRING"),
    ]),
    ("Seeder", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
    ]),

    # ── Routes ──────────────────────────────────────────────────────────────
    ("Route", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("http_method", "STRING"),
        ("uri", "STRING"),
        ("controller_fqn", "STRING"),
        ("action_method", "STRING"),
        ("middleware_stack", "STRING"),  # JSON list
        ("route_file", "STRING"),
        ("prefix", "STRING"),
        ("domain", "STRING"),
        ("wheres", "STRING"),   # JSON constraints
        ("rate_limit", "STRING"),
        ("is_api", "BOOLEAN"),
    ]),

    # ── Blade Templates ─────────────────────────────────────────────────────
    ("BladeTemplate", [
        ("node_id", "STRING"),
        ("name", "STRING"),   # view name (dot notation)
        ("file_path", "STRING"),
        ("relative_path", "STRING"),
        ("extends_layout", "STRING"),
        ("sections", "STRING"),   # JSON list
        ("stacks", "STRING"),     # JSON list
        ("slots", "STRING"),      # JSON list
    ]),
    ("BladeComponent", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("tag", "STRING"),       # x-component-name
        ("class_fqn", "STRING"),
        ("file_path", "STRING"),
        ("props", "STRING"),     # JSON list
        ("is_anonymous", "BOOLEAN"),
    ]),
    ("LivewireComponent", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("fqn", "STRING"),
        ("file_path", "STRING"),
        ("blade_view", "STRING"),
    ]),

    # ── Database Schema ──────────────────────────────────────────────────────
    ("Migration", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("file_path", "STRING"),
        ("batch", "INT32"),
        ("ran_at", "STRING"),
    ]),
    ("DatabaseTable", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("created_in", "STRING"),  # migration file path
        ("engine", "STRING"),
        ("charset", "STRING"),
    ]),
    ("DatabaseColumn", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("table_name", "STRING"),
        ("type", "STRING"),
        ("nullable", "BOOLEAN"),
        ("default_value", "STRING"),
        ("unique", "BOOLEAN"),
        ("indexed", "BOOLEAN"),
        ("unsigned", "BOOLEAN"),
        ("length", "INT32"),
    ]),

    # ── Container / Config ───────────────────────────────────────────────────
    ("ServiceBinding", [
        ("node_id", "STRING"),
        ("abstract", "STRING"),      # interface or alias being bound
        ("concrete", "STRING"),      # implementation FQN
        ("binding_type", "STRING"),  # singleton|transient|instance|contextual|tagged
        ("provider_fqn", "STRING"),
        ("file_path", "STRING"),
        ("line", "INT32"),
    ]),
    ("ConfigKey", [
        ("node_id", "STRING"),
        ("key", "STRING"),           # e.g. "app.name"
        ("file_path", "STRING"),
        ("default_value", "STRING"),
    ]),
    ("EnvVariable", [
        ("node_id", "STRING"),
        ("name", "STRING"),          # e.g. "APP_KEY"
        ("default_value", "STRING"),
        ("has_default", "BOOLEAN"),
    ]),

    # ── Scheduling ──────────────────────────────────────────────────────────
    ("ScheduledTask", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("command", "STRING"),
        ("frequency", "STRING"),
        ("cron_expression", "STRING"),
        ("timezone", "STRING"),
        ("without_overlapping", "BOOLEAN"),
        ("on_one_server", "BOOLEAN"),
        ("in_background", "BOOLEAN"),
        ("file_path", "STRING"),
        ("line", "INT32"),
    ]),

    # ── Analysis Constructs ─────────────────────────────────────────────────
    ("Community", [
        ("node_id", "STRING"),
        ("community_id", "INT32"),
        ("size", "INT32"),
        ("label", "STRING"),   # auto-generated descriptive label
    ]),
    ("Process", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("entry_type", "STRING"),  # route|command|job|listener|schedule
        ("entry_fqn", "STRING"),
        ("depth", "INT32"),
    ]),
]

# ── Relationship type definitions ─────────────────────────────────────────────
# Format: (rel_label, [(from_node, to_node), ...], [(prop_name, kuzu_type)...])
# KuzuDB 0.11.3 requires explicit FROM/TO node type pairs (no FROM ANY TO ANY).

REL_TYPES: list[tuple[str, list[tuple[str, str]], list[tuple[str, str]]]] = [
    # Filesystem
    ("CONTAINS", [
        ("Folder", "Folder"),
        ("Folder", "File"),
    ], []),
    ("DEFINES", [
        ("File", "Class_"),
        ("File", "Trait_"),
        ("File", "Interface_"),
        ("File", "Enum_"),
        ("File", "Function_"),
        ("Class_", "Method"),
        ("Trait_", "Method"),
        ("Interface_", "Method"),
        ("Class_", "Property"),
        ("Class_", "Constant"),
        ("Enum_", "Constant"),
    ], [
        ("symbol_type", "STRING"),
        ("line_start", "INT32"),
    ]),

    # PHP structure
    ("EXTENDS_CLASS", [
        ("Class_", "Class_"),
    ], []),
    ("IMPLEMENTS_INTERFACE", [
        ("Class_", "Interface_"),
    ], []),
    ("USES_TRAIT", [
        ("Class_", "Trait_"),
        ("Trait_", "Trait_"),
    ], [("line", "INT32")]),
    ("IMPORTS", [
        ("File", "File"),           # phase_04 creates File→File IMPORTS edges
        ("File", "Class_"),
        ("File", "Interface_"),
        ("File", "Trait_"),
        ("File", "Enum_"),
        ("File", "Function_"),
        ("Namespace", "Namespace"),
    ], [
        ("alias", "STRING"),
        ("symbols", "STRING"),  # JSON list
        ("line", "INT32"),
    ]),
    ("CALLS", [
        ("Method", "Method"),
        ("Method", "Function_"),
        ("Method", "Class_"),       # constructor calls, static calls
        ("Method", "EloquentModel"),
        ("Function_", "Function_"),
        ("Function_", "Method"),
        ("Function_", "Class_"),
    ], [
        ("confidence", "FLOAT"),
        ("call_type", "STRING"),  # direct|facade|container|magic|chain
        ("line", "INT32"),
    ]),
    ("USES_TYPE", [
        ("Method", "Class_"),
        ("Method", "Interface_"),
        ("Method", "Enum_"),
        ("Method", "Trait_"),
        ("Function_", "Class_"),
        ("Function_", "Interface_"),
        ("Function_", "Enum_"),
        ("Property", "Class_"),
        ("Property", "Interface_"),
        ("Property", "Enum_"),
        ("Class_", "Class_"),       # class-level property type declarations
        ("Class_", "Interface_"),
    ], [
        ("role", "STRING"),  # param|return|property|variable
        ("line", "INT32"),
    ]),

    # Laravel routing
    ("ROUTES_TO", [
        ("Route", "Method"),
        ("Route", "Controller"),
    ], [
        ("http_method", "STRING"),
        ("uri", "STRING"),
    ]),
    ("APPLIES_MIDDLEWARE", [
        ("Route", "Middleware"),
    ], [
        ("middleware_order", "INT32"),  # 'order' is reserved in KuzuDB
        ("parameters", "STRING"),  # JSON
    ]),

    # Eloquent
    ("HAS_RELATIONSHIP", [
        ("EloquentModel", "EloquentModel"),
    ], [
        ("relationship_type", "STRING"),  # hasMany|belongsTo|...
        ("foreign_key", "STRING"),
        ("local_key", "STRING"),
        ("pivot_table", "STRING"),
        ("method_name", "STRING"),
        ("is_polymorphic", "BOOLEAN"),
        ("morphable_type", "STRING"),
    ]),

    # Service container
    ("BINDS_TO", [
        ("ServiceBinding", "Class_"),
        ("ServiceBinding", "Interface_"),
        ("ServiceBinding", "Trait_"),
    ], [
        ("binding_type", "STRING"),
        ("contextual_for", "STRING"),
    ]),

    # Events / Jobs
    ("DISPATCHES", [
        ("Method", "Event"),
        ("Method", "Job"),
        ("Method", "Notification"),
        ("Function_", "Event"),
        ("Function_", "Job"),
    ], [
        ("dispatch_type", "STRING"),  # event|job|notification
        ("is_queued", "BOOLEAN"),
        ("line", "INT32"),
    ]),
    ("LISTENS_TO", [
        ("Listener", "Event"),
    ], []),
    ("HANDLES", [
        ("Listener", "Event"),
        ("Job", "Event"),
    ], [("queue", "STRING")]),
    ("NOTIFIES", [
        ("Method", "Notification"),
        ("Function_", "Notification"),
    ], [("channels", "STRING")]),

    # Blade
    ("RENDERS_TEMPLATE", [
        ("Method", "BladeTemplate"),
        ("Controller", "BladeTemplate"),
        ("Class_", "BladeTemplate"),
    ], [("line", "INT32")]),
    ("INCLUDES_TEMPLATE", [
        ("BladeTemplate", "BladeTemplate"),
    ], [("line", "INT32")]),
    ("EXTENDS_TEMPLATE", [
        ("BladeTemplate", "BladeTemplate"),
    ], []),
    ("HAS_COMPONENT", [
        ("BladeTemplate", "BladeComponent"),
        ("BladeTemplate", "LivewireComponent"),
    ], [("tag", "STRING"), ("line", "INT32")]),

    # Database
    ("MIGRATES_TABLE", [
        ("Migration", "DatabaseTable"),
    ], []),
    ("HAS_COLUMN", [
        ("DatabaseTable", "DatabaseColumn"),
    ], []),
    ("REFERENCES_TABLE", [
        ("DatabaseColumn", "DatabaseTable"),
        ("DatabaseTable", "DatabaseTable"),
    ], [
        ("from_column", "STRING"),
        ("to_column", "STRING"),
        ("on_delete", "STRING"),
        ("on_update", "STRING"),
    ]),

    # Config / Env
    ("USES_CONFIG", [
        ("Method", "ConfigKey"),
        ("Function_", "ConfigKey"),
        ("Class_", "ConfigKey"),
        ("File", "ConfigKey"),
    ], [("key", "STRING"), ("line", "INT32")]),
    ("USES_ENV", [
        ("Method", "EnvVariable"),
        ("Function_", "EnvVariable"),
        ("Class_", "EnvVariable"),
        ("File", "EnvVariable"),
    ], [("variable", "STRING"), ("line", "INT32")]),

    # Dependency injection
    ("INJECTS", [
        ("Class_", "Class_"),
        ("Class_", "Interface_"),
        ("Method", "Class_"),
        ("Method", "Interface_"),
    ], [
        ("injection_method", "STRING"),  # constructor|method|container
        ("parameter", "STRING"),
        ("type_hint", "STRING"),
    ]),

    # Authorization
    ("AUTHORIZES_WITH", [
        ("Method", "Policy"),
    ], [("ability", "STRING"), ("line", "INT32")]),
    ("VALIDATES_WITH", [
        ("Method", "FormRequest"),
        ("Controller", "FormRequest"),
        ("Method", "Class_"),       # when validating with Illuminate\Http\Request directly
    ], [("line", "INT32")]),
    ("TRANSFORMS_WITH", [
        ("Method", "Resource"),
        ("Controller", "Resource"),
    ], [("line", "INT32")]),
    ("SCHEDULES", [
        ("ServiceProvider", "ScheduledTask"),
        ("Command", "ScheduledTask"),
    ], [("frequency", "STRING")]),

    # Analysis
    ("MEMBER_OF", [
        ("Class_", "Community"),
        ("Method", "Community"),
        ("Function_", "Community"),
        ("Trait_", "Community"),
        ("Interface_", "Community"),
    ], []),  # symbol → Community
    ("STEP_IN_PROCESS", [
        ("Method", "Process"),
        ("Function_", "Process"),
    ], [("depth", "INT32"), ("step_order", "INT32")]),  # 'order' is reserved in KuzuDB
    ("COUPLED_WITH", [
        ("File", "File"),
    ], [
        ("strength", "FLOAT"),
        ("co_changes", "INT32"),
        ("period_months", "INT32"),
    ]),

    # Observers / Policies / Factories
    ("OBSERVES", [
        ("Observer", "EloquentModel"),
    ], []),
    ("AUTHORIZES_MODEL", [
        ("Policy", "EloquentModel"),
    ], []),
    ("DEFINES_FACTORY", [
        ("Factory", "EloquentModel"),
    ], []),
]


def node_id(label: str, *parts: str) -> str:
    """Generate a deterministic, human-readable node ID.

    Examples:
        node_id("method", "App\\\\Http\\\\Controllers\\\\UserController", "store")
        → "method:App\\Http\\Controllers\\UserController::store"

        node_id("route", "api.users.index")
        → "route:api.users.index"
    """
    clean = [p.replace("\\\\", "\\") for p in parts]
    return f"{label}:{('::'.join(clean))}"
