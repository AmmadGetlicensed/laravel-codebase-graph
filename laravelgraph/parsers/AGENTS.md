# AGENTS.md — laravelgraph/parsers/

## OVERVIEW

PHP/Blade/Composer parsers. `php.py` is the primary workhorse — tree-sitter AST with regex fallback. These parsers produce intermediate data structures consumed by pipeline phases; they do not write to the graph.

## STRUCTURE

```
parsers/
├── php.py        # PHPParser — tree-sitter + regex fallback; returns PHPFile dataclass
├── blade.py      # BladeParser — regex-based; returns BladeParsed dataclass
└── composer.py   # parse_composer() — reads composer.json; returns ComposerInfo
```

## Key Types

**`PHPFile`** (from `php.py`): Contains `classes`, `functions`, `namespaces`, `uses` (import statements), `calls`. Each class has `methods`, `properties`, `docblock`, `line_number`.

**`BladeParsed`** (from `blade.py`): Contains `extends`, `includes`, `components`, `livewire_components`, `sections`.

**`ComposerInfo`** (from `composer.py`): `laravel_version`, `php_constraint`, `psr4_map` (autoload namespace → path dict), `require` dict.

## Parsing Strategy

`php.py` tries tree-sitter first; falls back to regex if tree-sitter fails (e.g., syntax errors in PHP file). The fallback is intentionally lossy — it captures class/method names but not call graphs.

To detect which strategy was used: `PHPFile.parse_strategy` is `"tree-sitter"` or `"regex"`.

## CONVENTIONS

- Parsers are **stateless** — create a new instance or call the parse function per file.
- Return `None` (not raise) on unparseable files — callers check for None.
- Use `from __future__ import annotations` in all modules.

## ANTI-PATTERNS

- **Do not import tree-sitter at module level** — it may not be available in all environments; import inside the function.
- **Do not cache parsed results in parser classes** — caching is in `PipelineContext.parsed_php`.
- **Do not write to `PipelineContext` from parsers** — parsers are pure functions/classes; phases handle context writes.
