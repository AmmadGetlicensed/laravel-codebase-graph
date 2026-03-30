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
        ("changed_recently", "BOOLEAN"),
        ("changed_in_commit", "STRING"),
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
        ("has_dynamic_table_ref", "BOOLEAN"),
        ("changed_recently", "BOOLEAN"),
        ("changed_in_commit", "STRING"),
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
    ("DatabaseConnection", [
        ("node_id", "STRING"),
        ("name", "STRING"),       # logical name, e.g. "default", "analytics"
        ("driver", "STRING"),     # mysql | pgsql
        ("host", "STRING"),
        ("port", "INT32"),
        ("database", "STRING"),   # schema name
    ]),
    ("DatabaseTable", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("connection", "STRING"),  # which DB connection (empty = migration-derived)
        ("created_in", "STRING"),  # migration file path (empty = live-introspected)
        ("engine", "STRING"),
        ("charset", "STRING"),
        ("table_comment", "STRING"),
        ("source", "STRING"),      # "migration" | "live_db"
        ("row_count", "INT64"),    # approximate row count from information_schema.TABLE_ROWS
    ]),
    ("DatabaseColumn", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("table_name", "STRING"),
        ("connection", "STRING"),  # which DB connection
        ("type", "STRING"),
        ("full_type", "STRING"),   # full MySQL type e.g. varchar(255), enum('a','b')
        ("nullable", "BOOLEAN"),
        ("default_value", "STRING"),
        ("unique", "BOOLEAN"),
        ("indexed", "BOOLEAN"),
        ("unsigned", "BOOLEAN"),
        ("length", "INT64"),       # INT64 — LONGTEXT/LONGBLOB CHARACTER_MAXIMUM_LENGTH = 4294967295
        ("column_comment", "STRING"),
        ("extra", "STRING"),       # e.g. "auto_increment", "on update CURRENT_TIMESTAMP"
        ("column_key", "STRING"),  # PRI | UNI | MUL | ""
        # ── Static analysis evidence (populated by phase_26) ──────────────────
        ("write_path_evidence", "STRING"),  # JSON: [{method_fqn, line, rhs, context}]
        ("polymorphic_candidate", "BOOLEAN"),
        ("sibling_type_column", "STRING"),  # companion *_type column name if detected
        ("guard_conditions", "STRING"),     # JSON: [{condition_var, condition_val, method_fqn, line}]
    ]),
    ("InferredRelationship", [
        ("node_id", "STRING"),
        ("from_table", "STRING"),
        ("from_column", "STRING"),
        ("to_table", "STRING"),      # inferred target table
        ("to_column", "STRING"),     # inferred target column (usually 'id')
        ("connection", "STRING"),
        ("confidence", "FLOAT"),
        ("evidence_types", "STRING"),   # JSON list: write_path|guard_pattern|column_pair|naming
        ("conditions", "STRING"),       # JSON list: [{when_var, when_val}]
        ("evidence_summary", "STRING"), # human-readable for LLM prompt augmentation
    ]),
    ("StoredProcedure", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("connection", "STRING"),
        ("database", "STRING"),
        ("routine_type", "STRING"),   # PROCEDURE | FUNCTION
        ("parameters", "STRING"),     # JSON list of param definitions
        ("body_preview", "STRING"),   # first 1000 chars of body
        ("full_body", "STRING"),      # complete body for SQL parsing
        ("comment", "STRING"),
        ("security_type", "STRING"),
        ("definer", "STRING"),
        ("last_altered", "STRING"),   # ISO timestamp from information_schema
        ("created_at", "STRING"),     # ISO timestamp from information_schema
    ]),
    ("DatabaseView", [
        ("node_id", "STRING"),
        ("name", "STRING"),
        ("connection", "STRING"),
        ("database", "STRING"),
        ("definition", "STRING"),     # SELECT statement
        ("is_updatable", "STRING"),
    ]),
    ("UsageContext", [
        ("node_id", "STRING"),
        ("source_fqn", "STRING"),     # method/function/procedure FQN
        ("source_type", "STRING"),    # method | function | procedure | view
        ("tables_read", "STRING"),    # JSON list of table names
        ("tables_written", "STRING"), # JSON list of table names
        ("column_semantics", "STRING"), # JSON: {col: {when, references, meaning}}
        ("summary", "STRING"),        # LLM-generated natural language description
        ("confidence", "FLOAT"),
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

    # ── Feature Clusters (phase 27) ─────────────────────────────────────────
    ("Feature", [
        ("node_id", "STRING"),
        ("name", "STRING"),           # human-readable e.g. "Order Course"
        ("slug", "STRING"),           # machine key e.g. "order_course"
        ("route_prefix", "STRING"),   # primary URI segment used for grouping
        ("symbol_count", "INT32"),    # total symbols linked to this feature
        ("entry_routes", "STRING"),   # JSON list of route URIs
        ("has_changes", "BOOLEAN"),   # set by phase 29 if feature has recent git changes
    ]),

    # ── Behavioral Contracts (phase 28) ─────────────────────────────────────
    ("Contract", [
        ("node_id", "STRING"),
        ("name", "STRING"),           # e.g. "OrderRequest validation"
        ("contract_type", "STRING"),  # validation|authorization|lifecycle|mass_assignment
        ("source_class", "STRING"),   # short class name
        ("source_fqn", "STRING"),     # fully-qualified class name
        ("rules", "STRING"),          # JSON: {field: rules_string} or {method: bool} etc.
        ("file_path", "STRING"),
        ("line_start", "INT32"),
    ]),

    # ── Test Coverage (phase 30) ─────────────────────────────────────────────
    ("TestCase", [
        ("node_id", "STRING"),
        ("name", "STRING"),           # test file stem, e.g. "UserTest"
        ("fqn", "STRING"),            # PSR-4 FQN, e.g. "Tests\\Feature\\UserTest"
        ("file_path", "STRING"),
        ("test_type", "STRING"),      # feature|unit|integration
        ("covers_routes", "STRING"),  # JSON list of extracted URIs
        ("covers_classes", "STRING"), # JSON list of referenced class short names
    ]),

    # ── Performance Risks (phase 31) ─────────────────────────────────────────
    ("PerformanceRisk", [
        ("node_id", "STRING"),
        ("risk_type", "STRING"),      # n_plus_one|missing_eager_load|repeated_count|raw_query_bypass
        ("description", "STRING"),
        ("severity", "STRING"),       # HIGH|MEDIUM|LOW
        ("file_path", "STRING"),
        ("line_number", "INT32"),
        ("method_fqn", "STRING"),
        ("evidence", "STRING"),       # short human-readable evidence snippet
    ]),

    # ── External HTTP Client Calls (phase 32) ────────────────────────────────
    ("HttpClientCall", [
        ("node_id", "STRING"),
        ("caller_fqn", "STRING"),     # method/function that makes the call
        ("http_verb", "STRING"),      # GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS
        ("url_pattern", "STRING"),    # static URL or best-effort extracted pattern
        ("client_type", "STRING"),    # laravel_http|guzzle|curl|symfony_http
        ("file_path", "STRING"),
        ("line_number", "INT32"),
    ]),

    # ── Authorization Gates (phase 34) ──────────────────────────────────────
    ("Gate", [
        ("node_id", "STRING"),
        ("name", "STRING"),           # gate ability name, e.g. "update-post"
        ("callback_class", "STRING"), # class FQN implementing the gate, if any
        ("file_path", "STRING"),
        ("line_number", "INT32"),
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
        ("Route", "Class_"),    # closure/invokable controller resolved to class
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
        ("Listener", "Job"),    # listener handle() dispatches a job
        ("Listener", "Event"),  # listener handle() re-dispatches an event
    ], [
        ("dispatch_type", "STRING"),  # event|job|notification
        ("is_queued", "BOOLEAN"),
        ("line", "INT32"),
        ("condition", "STRING"),  # guarding if/case text, e.g. "$plan == 1"
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
        ("Listener", "Notification"),  # listener handle() sends a notification
    ], [("channels", "STRING")]),

    # Blade
    ("BLADE_CALLS", [
        ("BladeTemplate", "Method"),
        ("BladeTemplate", "Function_"),
    ], [("line", "INT32")]),
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

    # Database — schema structure
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
        ("constraint_name", "STRING"),
        ("enforced", "BOOLEAN"),  # false = live FK vs migration-derived guess
    ]),
    ("HAS_TABLE", [
        ("DatabaseConnection", "DatabaseTable"),
    ], []),
    ("HAS_PROCEDURE", [
        ("DatabaseConnection", "StoredProcedure"),
    ], []),
    ("HAS_VIEW", [
        ("DatabaseConnection", "DatabaseView"),
    ], []),

    # Database — code linkage
    ("USES_TABLE", [
        ("EloquentModel", "DatabaseTable"),
    ], [
        ("connection", "STRING"),
    ]),
    # Universal DB access: covers both Eloquent ad-hoc usage and raw query builder
    ("QUERIES_TABLE", [
        ("Method", "DatabaseTable"),
        ("Function_", "DatabaseTable"),
    ], [
        ("operation", "STRING"),    # read | write | readwrite | call
        ("connection", "STRING"),
        ("via", "STRING"),          # eloquent | query_builder | raw_sql | procedure
        ("confidence", "FLOAT"),
        ("line", "INT32"),
    ]),

    # Database — procedure access
    ("PROCEDURE_READS", [
        ("StoredProcedure", "DatabaseTable"),
    ], [
        ("confidence", "FLOAT"),
    ]),
    ("PROCEDURE_WRITES", [
        ("StoredProcedure", "DatabaseTable"),
    ], [
        ("confidence", "FLOAT"),
    ]),

    # Database — relationship inference
    ("INFERRED_REFERENCES", [
        ("DatabaseColumn", "DatabaseTable"),
    ], [
        ("confidence", "FLOAT"),
        ("condition", "STRING"),      # "" = unconditional, else "when type='order'"
        ("evidence_type", "STRING"),  # write_path | guard_pattern | column_pair | naming
        ("evidence_detail", "STRING"),
    ]),

    # Database — usage context (semantic layer)
    ("HAS_USAGE_CONTEXT", [
        ("Method", "UsageContext"),
        ("Function_", "UsageContext"),
        ("StoredProcedure", "UsageContext"),
    ], []),
    ("CONTEXT_READS", [
        ("UsageContext", "DatabaseColumn"),
        ("UsageContext", "DatabaseTable"),
    ], [("confidence", "FLOAT")]),
    ("CONTEXT_WRITES", [
        ("UsageContext", "DatabaseColumn"),
        ("UsageContext", "DatabaseTable"),
    ], [("confidence", "FLOAT")]),

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
        ("File", "File"),       # self-referential gate/policy usage noted at file level
    ], [("ability", "STRING"), ("line", "INT32")]),
    ("VALIDATES_WITH", [
        ("Method", "FormRequest"),
        ("Controller", "FormRequest"),
        ("Method", "Class_"),       # when validating with Illuminate\Http\Request directly
    ], [("line", "INT32")]),
    ("TRANSFORMS_WITH", [
        ("Method", "Resource"),
        ("Controller", "Resource"),
        ("Method", "Class_"),   # resource class resolved as generic class (no Resource node yet)
    ], [("line", "INT32")]),
    ("SCHEDULES", [
        ("ServiceProvider", "ScheduledTask"),
        ("Command", "ScheduledTask"),
        ("ScheduledTask", "Job"),      # scheduled task runs this job class
        ("ScheduledTask", "Command"),  # scheduled task runs this artisan command
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

    # Feature clustering (phase 27)
    ("BELONGS_TO_FEATURE", [
        ("Route", "Feature"),
        ("EloquentModel", "Feature"),
        ("Class_", "Feature"),
        ("Event", "Feature"),
        ("Job", "Feature"),
        ("Controller", "Feature"),
        ("FormRequest", "Feature"),
        ("Policy", "Feature"),
        ("Observer", "Feature"),
    ], [
        ("confidence", "FLOAT"),   # 1.0=route, 0.8=model, 0.7=class, 0.6=event/job
        ("match_type", "STRING"),  # exact|contains|prefix
    ]),

    # Behavioral contracts (phase 28)
    ("GOVERNS", [
        ("Contract", "Route"),
        ("Contract", "Class_"),
        ("Contract", "EloquentModel"),
        ("Contract", "FormRequest"),
        ("Contract", "Policy"),
        ("Contract", "Observer"),
    ], [
        ("role", "STRING"),  # validates|authorizes|observes|fillable|guarded
    ]),

    # Test coverage (phase 30)
    ("TESTS", [
        ("TestCase", "Route"),
        ("TestCase", "Class_"),
        ("TestCase", "Method"),
    ], []),

    # Performance risks (phase 31)
    ("HAS_PERFORMANCE_RISK", [
        ("Method", "PerformanceRisk"),
    ], []),

    # External HTTP calls (phase 32)
    ("CALLS_EXTERNAL", [
        ("Method", "HttpClientCall"),
        ("Function_", "HttpClientCall"),
    ], [
        ("http_verb", "STRING"),
        ("line", "INT32"),
    ]),

    # Authorization gates (phase 34)
    ("DEFINES_GATE", [
        ("ServiceProvider", "Gate"),
        ("Class_", "Gate"),
        ("File", "Gate"),
    ], [("line", "INT32")]),
    ("CHECKS_GATE", [
        ("Method", "Gate"),
        ("Function_", "Gate"),
    ], [("ability", "STRING"), ("line", "INT32")]),
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
