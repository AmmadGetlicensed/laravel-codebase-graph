"""PHP AST parser using tree-sitter-php.

Extracts:
- Namespaces, use statements
- Classes, traits, interfaces, enums
- Methods, functions, properties, constants
- Call expressions
- Laravel-specific patterns (facades, Eloquent relationships, etc.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from laravelgraph.logging import get_logger

logger = get_logger(__name__)

# Lazy-initialize tree-sitter to avoid import errors if not installed
_PHP_LANGUAGE = None
_PARSER = None


def _get_parser():
    global _PHP_LANGUAGE, _PARSER
    if _PARSER is None:
        try:
            import tree_sitter_php as tsphp
            from tree_sitter import Language, Parser

            _PHP_LANGUAGE = Language(tsphp.language_php())
            _PARSER = Parser(_PHP_LANGUAGE)
        except ImportError:
            logger.warning("tree-sitter-php not available; falling back to regex parser")
            _PARSER = False  # Sentinel: unavailable
    return _PARSER


# ── Data classes for parsed output ────────────────────────────────────────────

@dataclass
class ParsedNamespace:
    name: str
    line: int


@dataclass
class ParsedUse:
    fqn: str
    alias: str
    line: int


@dataclass
class ParsedClass:
    name: str
    fqn: str
    line_start: int
    line_end: int
    extends: str | None
    implements: list[str]
    traits: list[str]
    is_abstract: bool
    is_final: bool
    methods: list["ParsedMethod"]
    properties: list["ParsedProperty"]
    constants: list["ParsedConstant"]
    docblock: str
    attributes: list[str]  # PHP 8 attributes


@dataclass
class ParsedTrait:
    name: str
    fqn: str
    line_start: int
    line_end: int
    methods: list["ParsedMethod"]
    traits: list[str]


@dataclass
class ParsedInterface:
    name: str
    fqn: str
    line_start: int
    line_end: int
    extends: list[str]
    methods: list["ParsedMethod"]


@dataclass
class ParsedEnum:
    name: str
    fqn: str
    line_start: int
    line_end: int
    backed_type: str  # "string" | "int" | ""
    cases: list[str]
    implements: list[str]


@dataclass
class ParsedMethod:
    name: str
    line_start: int
    line_end: int
    visibility: str  # public|protected|private
    is_static: bool
    is_abstract: bool
    return_type: str
    params: list["ParsedParam"]
    calls: list["ParsedCall"]
    docblock: str
    attributes: list[str]


@dataclass
class ParsedParam:
    name: str
    type_hint: str
    default: str
    is_promoted: bool


@dataclass
class ParsedProperty:
    name: str
    line: int
    visibility: str
    type_hint: str
    is_static: bool
    default_value: str


@dataclass
class ParsedConstant:
    name: str
    line: int
    value: str
    visibility: str


@dataclass
class ParsedCall:
    receiver: str | None   # None for global calls
    method: str
    args: list[str]
    line: int
    is_static: bool
    raw: str


@dataclass
class ParsedFunction:
    name: str
    fqn: str
    line_start: int
    line_end: int
    return_type: str
    params: list[ParsedParam]
    calls: list[ParsedCall]
    docblock: str


@dataclass
class PHPFile:
    path: str
    namespace: str
    uses: list[ParsedUse]
    classes: list[ParsedClass]
    traits: list[ParsedTrait]
    interfaces: list[ParsedInterface]
    enums: list[ParsedEnum]
    functions: list[ParsedFunction]
    errors: list[str] = field(default_factory=list)


# ── Laravel-specific constants ────────────────────────────────────────────────

# Eloquent relationship methods
ELOQUENT_RELATIONSHIPS = {
    "hasOne", "hasMany", "belongsTo", "belongsToMany",
    "hasOneThrough", "hasManyThrough", "morphOne", "morphMany",
    "morphTo", "morphToMany", "morphedByMany",
}

# Laravel Facade → concrete class mapping (commonly used ones)
FACADE_MAP: dict[str, str] = {
    "Cache": "Illuminate\\Cache\\Repository",
    "Config": "Illuminate\\Config\\Repository",
    "DB": "Illuminate\\Database\\DatabaseManager",
    "Event": "Illuminate\\Events\\Dispatcher",
    "Gate": "Illuminate\\Auth\\Access\\Gate",
    "Hash": "Illuminate\\Hashing\\HashManager",
    "Http": "Illuminate\\Http\\Client\\Factory",
    "Log": "Illuminate\\Log\\LogManager",
    "Mail": "Illuminate\\Mail\\Mailer",
    "Notification": "Illuminate\\Notifications\\ChannelManager",
    "Queue": "Illuminate\\Queue\\QueueManager",
    "Redis": "Illuminate\\Redis\\RedisManager",
    "Route": "Illuminate\\Routing\\Router",
    "Schema": "Illuminate\\Database\\Schema\\Builder",
    "Session": "Illuminate\\Session\\SessionManager",
    "Storage": "Illuminate\\Filesystem\\FilesystemManager",
    "URL": "Illuminate\\Routing\\UrlGenerator",
    "Validator": "Illuminate\\Validation\\Factory",
    "View": "Illuminate\\View\\Factory",
    "Auth": "Illuminate\\Auth\\AuthManager",
    "Bus": "Illuminate\\Contracts\\Bus\\Dispatcher",
    "Broadcast": "Illuminate\\Broadcasting\\BroadcastManager",
    "Crypt": "Illuminate\\Encryption\\Encrypter",
    "File": "Illuminate\\Filesystem\\Filesystem",
    "Artisan": "Illuminate\\Contracts\\Console\\Kernel",
    "Response": "Illuminate\\Routing\\ResponseFactory",
    "Request": "Illuminate\\Http\\Request",
    "Cookie": "Illuminate\\Cookie\\CookieJar",
    "Redirect": "Illuminate\\Routing\\Redirector",
}

# PHP built-in call blocklist (noise filter)
CALL_BLOCKLIST = {
    # PHP language constructs
    "array_map", "array_filter", "array_reduce", "array_merge", "array_push",
    "array_pop", "array_shift", "array_unshift", "array_keys", "array_values",
    "array_key_exists", "array_search", "array_slice", "array_splice",
    "array_unique", "array_flip", "array_combine", "array_chunk", "array_diff",
    "array_intersect", "array_pad", "array_column", "array_fill",
    "count", "sizeof", "strlen", "str_len", "strtolower", "strtoupper",
    "substr", "strpos", "strrpos", "str_replace", "str_contains", "str_starts_with",
    "str_ends_with", "trim", "ltrim", "rtrim", "explode", "implode", "join",
    "sprintf", "printf", "intval", "floatval", "strval", "boolval",
    "is_null", "is_array", "is_string", "is_int", "is_float", "is_bool", "is_object",
    "isset", "empty", "unset", "in_array", "json_encode", "json_decode",
    "preg_match", "preg_replace", "preg_split", "preg_match_all",
    "date", "time", "strtotime", "mktime", "microtime",
    "class_exists", "method_exists", "property_exists", "function_exists",
    "get_class", "get_parent_class", "instanceof",
    "call_user_func", "call_user_func_array",
    "throw", "new", "echo", "print", "die", "exit",
    # Laravel helpers
    "app", "config", "env", "route", "url", "redirect", "response",
    "request", "session", "view", "trans", "__", "trans_choice",
    "abort", "abort_if", "abort_unless", "back",
    "collect", "data_get", "data_set", "optional", "rescue",
    "tap", "value", "with", "filled", "blank", "class_basename",
    "str", "now", "today", "yesterday", "tomorrow",
    "dispatch", "broadcast", "event", "info", "logger", "report", "report_if",
    "throw_if", "throw_unless", "validator",
}

# Laravel model scope pattern
SCOPE_PATTERN = re.compile(r"^scope[A-Z]")


# ── Parser class ──────────────────────────────────────────────────────────────

class PHPParser:
    """Parses PHP files using tree-sitter, with regex fallback."""

    def parse_file(self, path: Path) -> PHPFile:
        try:
            source = path.read_bytes()
        except OSError as e:
            return PHPFile(
                path=str(path),
                namespace="",
                uses=[],
                classes=[],
                traits=[],
                interfaces=[],
                enums=[],
                functions=[],
                errors=[str(e)],
            )

        parser = _get_parser()
        if parser and parser is not False:
            return self._parse_with_treesitter(str(path), source, parser)
        else:
            return self._parse_with_regex(str(path), source.decode("utf-8", errors="replace"))

    # ── tree-sitter parser ────────────────────────────────────────────────

    def _parse_with_treesitter(self, path: str, source: bytes, parser) -> PHPFile:
        try:
            tree = parser.parse(source)
            visitor = _TSVisitor(source, path)
            visitor.visit(tree.root_node)
            return visitor.result()
        except Exception as e:
            logger.warning("tree-sitter parse error", path=path, error=str(e))
            return self._parse_with_regex(path, source.decode("utf-8", errors="replace"))

    # ── Regex fallback ────────────────────────────────────────────────────

    def _parse_with_regex(self, path: str, source: str) -> PHPFile:
        """Best-effort regex-based parsing when tree-sitter unavailable."""
        lines = source.splitlines()
        namespace = ""
        uses: list[ParsedUse] = []
        classes: list[ParsedClass] = []
        traits: list[ParsedTrait] = []
        interfaces: list[ParsedInterface] = []
        enums: list[ParsedEnum] = []
        functions: list[ParsedFunction] = []
        errors: list[str] = []

        ns_match = re.search(r"^namespace\s+([\w\\]+)\s*;", source, re.MULTILINE)
        if ns_match:
            namespace = ns_match.group(1)

        for i, line in enumerate(lines, 1):
            # Use statements
            use_m = re.match(r"^\s*use\s+([\w\\]+)(?:\s+as\s+(\w+))?\s*;", line)
            if use_m:
                fqn = use_m.group(1)
                alias = use_m.group(2) or fqn.split("\\")[-1]
                uses.append(ParsedUse(fqn=fqn, alias=alias, line=i))

            # Classes
            class_m = re.match(
                r"^\s*(abstract\s+|final\s+)?class\s+(\w+)"
                r"(?:\s+extends\s+([\w\\]+))?(?:\s+implements\s+([\w\\,\s]+))?\s*\{?",
                line,
            )
            if class_m and "interface" not in line and "trait" not in line:
                modifiers = (class_m.group(1) or "").strip()
                name = class_m.group(2)
                extends = class_m.group(3)
                implements_str = class_m.group(4) or ""
                implements = [s.strip() for s in implements_str.split(",") if s.strip()]
                fqn = f"{namespace}\\{name}" if namespace else name

                # Find methods inside this class (rough)
                methods = self._extract_methods(source, lines, i)
                classes.append(ParsedClass(
                    name=name, fqn=fqn, line_start=i, line_end=i + 50,
                    extends=extends, implements=implements, traits=[],
                    is_abstract="abstract" in modifiers, is_final="final" in modifiers,
                    methods=methods, properties=[], constants=[], docblock="", attributes=[],
                ))

            # Traits
            trait_m = re.match(r"^\s*trait\s+(\w+)\s*\{?", line)
            if trait_m:
                name = trait_m.group(1)
                fqn = f"{namespace}\\{name}" if namespace else name
                traits.append(ParsedTrait(
                    name=name, fqn=fqn, line_start=i, line_end=i + 50, methods=[], traits=[],
                ))

            # Interfaces
            iface_m = re.match(r"^\s*interface\s+(\w+)(?:\s+extends\s+([\w\\,\s]+))?\s*\{?", line)
            if iface_m:
                name = iface_m.group(1)
                fqn = f"{namespace}\\{name}" if namespace else name
                ext_str = iface_m.group(2) or ""
                interfaces.append(ParsedInterface(
                    name=name, fqn=fqn, line_start=i, line_end=i + 20,
                    extends=[s.strip() for s in ext_str.split(",") if s.strip()],
                    methods=[],
                ))

            # Enums
            enum_m = re.match(r"^\s*enum\s+(\w+)(?::\s*(string|int))?\s*", line)
            if enum_m:
                name = enum_m.group(1)
                fqn = f"{namespace}\\{name}" if namespace else name
                enums.append(ParsedEnum(
                    name=name, fqn=fqn, line_start=i, line_end=i + 30,
                    backed_type=enum_m.group(2) or "", cases=[], implements=[],
                ))

        return PHPFile(
            path=path, namespace=namespace, uses=uses,
            classes=classes, traits=traits, interfaces=interfaces,
            enums=enums, functions=functions, errors=errors,
        )

    def _extract_methods(self, source: str, lines: list[str], class_start: int) -> list[ParsedMethod]:
        methods = []
        for i, line in enumerate(lines[class_start:], class_start + 1):
            m = re.match(
                r"^\s*(public|protected|private)?\s*(static\s+)?(?:abstract\s+)?function\s+(\w+)\s*\(",
                line,
            )
            if m:
                visibility = m.group(1) or "public"
                is_static = bool(m.group(2))
                name = m.group(3)
                calls = self._extract_calls(source, i, i + 30)
                methods.append(ParsedMethod(
                    name=name, line_start=i, line_end=i + 20,
                    visibility=visibility, is_static=is_static, is_abstract=False,
                    return_type="", params=[], calls=calls, docblock="", attributes=[],
                ))
        return methods

    def _extract_calls(self, source: str, line_start: int, line_end: int) -> list[ParsedCall]:
        calls = []
        lines = source.splitlines()[line_start - 1:line_end]
        for i, line in enumerate(lines, line_start):
            # Static calls: Foo::bar()
            for m in re.finditer(r"(\w+)::(\w+)\s*\(", line):
                receiver, method = m.group(1), m.group(2)
                if method not in CALL_BLOCKLIST and receiver not in ("self", "parent", "static"):
                    calls.append(ParsedCall(
                        receiver=receiver, method=method, args=[], line=i, is_static=True,
                        raw=m.group(0),
                    ))
            # Instance calls: $obj->method()
            for m in re.finditer(r"\$(\w+)->(\w+)\s*\(", line):
                receiver, method = m.group(1), m.group(2)
                if method not in CALL_BLOCKLIST:
                    calls.append(ParsedCall(
                        receiver=receiver, method=method, args=[], line=i, is_static=False,
                        raw=m.group(0),
                    ))
        return calls


# ── tree-sitter AST visitor ───────────────────────────────────────────────────

class _TSVisitor:
    """Walks tree-sitter PHP AST and extracts structured data."""

    def __init__(self, source: bytes, path: str) -> None:
        self._src = source
        self._path = path
        self._namespace = ""
        self._uses: list[ParsedUse] = []
        self._classes: list[ParsedClass] = []
        self._traits: list[ParsedTrait] = []
        self._interfaces: list[ParsedInterface] = []
        self._enums: list[ParsedEnum] = []
        self._functions: list[ParsedFunction] = []
        self._errors: list[str] = []

    def _text(self, node: Any) -> str:
        return self._src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _line(self, node: Any) -> int:
        return node.start_point[0] + 1

    def visit(self, node: Any) -> None:
        if node.type == "ERROR":
            self._errors.append(f"Parse error at line {self._line(node)}")
        elif node.type == "namespace_definition":
            self._visit_namespace(node)
        elif node.type in ("use_declaration", "namespace_use_declaration"):
            self._visit_use(node)
        elif node.type == "class_declaration":
            docblock = self._get_preceding_docblock(node)
            cls = self._visit_class(node, docblock=docblock)
            if cls:
                self._classes.append(cls)
        elif node.type == "trait_declaration":
            docblock = self._get_preceding_docblock(node)
            t = self._visit_trait(node, docblock=docblock)
            if t:
                self._traits.append(t)
        elif node.type == "interface_declaration":
            iface = self._visit_interface(node)
            if iface:
                self._interfaces.append(iface)
        elif node.type == "enum_declaration":
            enum = self._visit_enum(node)
            if enum:
                self._enums.append(enum)
        elif node.type == "function_definition":
            docblock = self._get_preceding_docblock(node)
            fn = self._visit_function(node, docblock=docblock)
            if fn:
                self._functions.append(fn)

        for child in node.children:
            self.visit(child)

    def _get_preceding_docblock(self, node: Any) -> str:
        """Return the PHPDoc comment immediately preceding this node, if any."""
        try:
            prev = node.prev_sibling
            if prev and prev.type == "comment":
                text = self._text(prev)
                if text.startswith("/**"):
                    return text
        except Exception:
            pass
        return ""

    def _visit_namespace(self, node: Any) -> None:
        for child in node.children:
            if child.type == "namespace_name":
                self._namespace = self._text(child)
                break

    def _visit_use(self, node: Any) -> None:
        for clause in node.children:
            if clause.type == "use_instead_of_clause":
                continue
            if "use_clause" in clause.type:
                fqn = ""
                alias = ""
                for part in clause.children:
                    if part.type == "qualified_name":
                        fqn = self._text(part)
                    elif part.type == "name":
                        alias = self._text(part)
                if fqn:
                    if not alias:
                        alias = fqn.split("\\")[-1]
                    self._uses.append(ParsedUse(fqn=fqn, alias=alias, line=self._line(node)))

    def _visit_class(self, node: Any, docblock: str = "") -> ParsedClass | None:
        name = ""
        extends = None
        implements: list[str] = []
        traits: list[str] = []
        is_abstract = False
        is_final = False
        methods: list[ParsedMethod] = []
        properties: list[ParsedProperty] = []
        constants: list[ParsedConstant] = []

        for child in node.children:
            t = child.type
            if t == "name":
                name = self._text(child)
            elif t == "abstract_modifier":
                is_abstract = True
            elif t == "final_modifier":
                is_final = True
            elif t == "base_clause":
                for c in child.children:
                    if c.type in ("name", "qualified_name"):
                        extends = self._text(c)
            elif t == "class_implements":
                for c in child.children:
                    if c.type in ("name", "qualified_name"):
                        implements.append(self._text(c))
            elif t == "declaration_list":
                pending_doc = ""
                for member in child.children:
                    mt = member.type
                    if mt == "comment":
                        text = self._text(member)
                        pending_doc = text if text.startswith("/**") else ""
                    elif mt == "method_declaration":
                        m = self._visit_method(member, docblock=pending_doc)
                        if m:
                            methods.append(m)
                        pending_doc = ""
                    elif mt == "property_declaration":
                        props = self._visit_properties(member)
                        properties.extend(props)
                        pending_doc = ""
                    elif mt == "const_declaration":
                        consts = self._visit_constants(member)
                        constants.extend(consts)
                        pending_doc = ""
                    elif mt == "use_declaration":
                        for c in member.children:
                            if c.type in ("name", "qualified_name"):
                                traits.append(self._text(c))
                        pending_doc = ""

        if not name:
            return None

        fqn = f"{self._namespace}\\{name}" if self._namespace else name
        return ParsedClass(
            name=name, fqn=fqn,
            line_start=self._line(node), line_end=node.end_point[0] + 1,
            extends=extends, implements=implements, traits=traits,
            is_abstract=is_abstract, is_final=is_final,
            methods=methods, properties=properties, constants=constants,
            docblock=docblock, attributes=[],
        )

    def _visit_trait(self, node: Any, docblock: str = "") -> ParsedTrait | None:
        name = ""
        methods: list[ParsedMethod] = []
        traits: list[str] = []

        for child in node.children:
            if child.type == "name":
                name = self._text(child)
            elif child.type == "declaration_list":
                pending_doc = ""
                for member in child.children:
                    mt = member.type
                    if mt == "comment":
                        text = self._text(member)
                        pending_doc = text if text.startswith("/**") else ""
                    elif mt == "method_declaration":
                        m = self._visit_method(member, docblock=pending_doc)
                        if m:
                            methods.append(m)
                        pending_doc = ""
                    elif mt == "use_declaration":
                        for c in member.children:
                            if c.type in ("name", "qualified_name"):
                                traits.append(self._text(c))
                        pending_doc = ""

        if not name:
            return None

        fqn = f"{self._namespace}\\{name}" if self._namespace else name
        return ParsedTrait(
            name=name, fqn=fqn,
            line_start=self._line(node), line_end=node.end_point[0] + 1,
            methods=methods, traits=traits,
        )

    def _visit_interface(self, node: Any) -> ParsedInterface | None:
        name = ""
        extends: list[str] = []
        methods: list[ParsedMethod] = []

        for child in node.children:
            if child.type == "name":
                name = self._text(child)
            elif child.type == "base_clause":
                for c in child.children:
                    if c.type in ("name", "qualified_name"):
                        extends.append(self._text(c))
            elif child.type == "declaration_list":
                for member in child.children:
                    if member.type == "method_declaration":
                        m = self._visit_method(member)
                        if m:
                            methods.append(m)

        if not name:
            return None

        fqn = f"{self._namespace}\\{name}" if self._namespace else name
        return ParsedInterface(
            name=name, fqn=fqn,
            line_start=self._line(node), line_end=node.end_point[0] + 1,
            extends=extends, methods=methods,
        )

    def _visit_enum(self, node: Any) -> ParsedEnum | None:
        name = ""
        backed_type = ""
        cases: list[str] = []
        implements: list[str] = []

        for child in node.children:
            if child.type == "name":
                name = self._text(child)
            elif child.type == "primitive_type":
                # Backed type — appears directly as a child of enum_declaration
                # (tree-sitter-php grammar: `enum Foo: string { ... }`)
                backed_type = self._text(child)
            elif child.type == "enum_backed_type":
                # Older grammar variant wraps it in enum_backed_type
                for c in child.children:
                    if c.type == "primitive_type":
                        backed_type = self._text(c)
            elif child.type == "class_implements":
                for c in child.children:
                    if c.type in ("name", "qualified_name"):
                        implements.append(self._text(c))
            elif child.type in ("declaration_list", "enum_declaration_list"):
                for member in child.children:
                    if member.type == "enum_case":
                        for c in member.children:
                            if c.type == "name":
                                cases.append(self._text(c))

        if not name:
            return None

        fqn = f"{self._namespace}\\{name}" if self._namespace else name
        return ParsedEnum(
            name=name, fqn=fqn,
            line_start=self._line(node), line_end=node.end_point[0] + 1,
            backed_type=backed_type, cases=cases, implements=implements,
        )

    def _visit_method(self, node: Any, docblock: str = "") -> ParsedMethod | None:
        name = ""
        visibility = "public"
        is_static = False
        is_abstract = False
        return_type = ""
        params: list[ParsedParam] = []
        calls: list[ParsedCall] = []

        for child in node.children:
            t = child.type
            if t == "name":
                name = self._text(child)
            elif t in ("public", "protected", "private"):
                visibility = t
            elif t == "static_modifier":
                is_static = True
            elif t == "abstract_modifier":
                is_abstract = True
            elif t == "named_type":
                return_type = self._text(child)
            elif t == "union_type":
                return_type = self._text(child)
            elif t == "formal_parameters":
                params = self._visit_params(child)
            elif t == "compound_statement":
                calls = self._collect_calls(child)

        if not name:
            return None

        return ParsedMethod(
            name=name, line_start=self._line(node), line_end=node.end_point[0] + 1,
            visibility=visibility, is_static=is_static, is_abstract=is_abstract,
            return_type=return_type, params=params, calls=calls, docblock=docblock, attributes=[],
        )

    def _visit_function(self, node: Any, docblock: str = "") -> ParsedFunction | None:
        name = ""
        return_type = ""
        params: list[ParsedParam] = []
        calls: list[ParsedCall] = []

        for child in node.children:
            t = child.type
            if t == "name":
                name = self._text(child)
            elif t in ("named_type", "union_type"):
                return_type = self._text(child)
            elif t == "formal_parameters":
                params = self._visit_params(child)
            elif t == "compound_statement":
                calls = self._collect_calls(child)

        if not name:
            return None

        fqn = f"{self._namespace}\\{name}" if self._namespace else name
        return ParsedFunction(
            name=name, fqn=fqn,
            line_start=self._line(node), line_end=node.end_point[0] + 1,
            return_type=return_type, params=params, calls=calls, docblock=docblock,
        )

    def _visit_params(self, node: Any) -> list[ParsedParam]:
        params = []
        for child in node.children:
            if child.type in ("simple_parameter", "variadic_parameter", "property_promotion_parameter"):
                name = ""
                type_hint = ""
                default = ""
                is_promoted = child.type == "property_promotion_parameter"
                for part in child.children:
                    pt = part.type
                    if pt == "variable_name":
                        name = self._text(part).lstrip("$")
                    elif pt in ("named_type", "union_type", "intersection_type", "nullable_type"):
                        type_hint = self._text(part)
                    elif pt in ("integer", "string", "boolean", "null", "float"):
                        default = self._text(part)
                if name:
                    params.append(ParsedParam(
                        name=name, type_hint=type_hint, default=default, is_promoted=is_promoted,
                    ))
        return params

    def _collect_calls(self, node: Any) -> list[ParsedCall]:
        """Recursively collect call expressions from a statement block."""
        calls = []
        self._walk_calls(node, calls)
        return calls

    def _walk_calls(self, node: Any, calls: list[ParsedCall]) -> None:
        if node.type in ("static_call_expression", "scoped_call_expression"):
            # tree-sitter-php uses "scoped_call_expression" for Foo::bar()
            receiver = ""
            method = ""
            for child in node.children:
                if child.type in ("name", "qualified_name", "variable_name"):
                    if not receiver:
                        receiver = self._text(child)
                    else:
                        method = self._text(child)
                elif child.type == "member_name":
                    method = self._text(child)
            if receiver and method and method not in CALL_BLOCKLIST:
                calls.append(ParsedCall(
                    receiver=receiver.lstrip("$"), method=method, args=[],
                    line=self._line(node), is_static=True, raw=self._text(node)[:100],
                ))

        elif node.type == "member_call_expression":
            receiver = ""
            method = ""
            for child in node.children:
                if child.type in ("variable_name",) and not receiver:
                    receiver = self._text(child).lstrip("$")
                elif child.type == "member_name":
                    method = self._text(child)
                elif child.type == "name" and not method:
                    method = self._text(child)
            if method and method not in CALL_BLOCKLIST:
                calls.append(ParsedCall(
                    receiver=receiver or "this", method=method, args=[],
                    line=self._line(node), is_static=False, raw=self._text(node)[:100],
                ))

        elif node.type == "function_call_expression":
            name = ""
            for child in node.children:
                if child.type in ("name", "qualified_name"):
                    name = self._text(child)
                    break
            if name and name not in CALL_BLOCKLIST:
                calls.append(ParsedCall(
                    receiver=None, method=name, args=[],
                    line=self._line(node), is_static=False, raw=self._text(node)[:100],
                ))

        for child in node.children:
            self._walk_calls(child, calls)

    def _visit_properties(self, node: Any) -> list[ParsedProperty]:
        props = []
        visibility = "public"
        type_hint = ""
        is_static = False
        for child in node.children:
            if child.type in ("public", "protected", "private"):
                visibility = child.type
            elif child.type == "static_modifier":
                is_static = True
            elif child.type in ("named_type", "union_type"):
                type_hint = self._text(child)
            elif child.type == "property_element":
                name = ""
                default = ""
                for c in child.children:
                    if c.type == "variable_name":
                        name = self._text(c).lstrip("$")
                    elif c.type in ("integer", "string", "true", "false", "null"):
                        default = self._text(c)
                if name:
                    props.append(ParsedProperty(
                        name=name, line=self._line(node),
                        visibility=visibility, type_hint=type_hint,
                        is_static=is_static, default_value=default,
                    ))
        return props

    def _visit_constants(self, node: Any) -> list[ParsedConstant]:
        consts = []
        visibility = "public"
        for child in node.children:
            if child.type in ("public", "protected", "private"):
                visibility = child.type
            elif child.type == "const_element":
                name = ""
                value = ""
                for c in child.children:
                    if c.type == "name":
                        name = self._text(c)
                    elif c.type not in ("=",):
                        value = self._text(c)[:100]
                if name:
                    consts.append(ParsedConstant(
                        name=name, line=self._line(node), value=value, visibility=visibility,
                    ))
        return consts

    def _visit_properties_from_member(self, member: Any) -> list[ParsedProperty]:
        return self._visit_properties(member)

    def result(self) -> PHPFile:
        return PHPFile(
            path=self._path,
            namespace=self._namespace,
            uses=self._uses,
            classes=self._classes,
            traits=self._traits,
            interfaces=self._interfaces,
            enums=self._enums,
            functions=self._functions,
            errors=self._errors,
        )
