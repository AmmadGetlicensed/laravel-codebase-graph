"""Plugin validator — static analysis enforcement before any plugin code runs."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from laravelgraph.logging import get_logger

logger = get_logger(__name__)

REQUIRED_MANIFEST_FIELDS = ("name", "version", "tool_prefix")


@dataclass
class _ValidatorResult:
    """Lightweight result returned by validate_plugin_file_content."""

    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

# Semver-ish: MAJOR.MINOR.PATCH
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Plugin names: alphanumeric and hyphens only
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*$")


class PluginValidationError(Exception):
    """Raised when a plugin file fails static validation."""

    def __init__(self, errors: list[str], warnings: list[str] | None = None) -> None:
        self.errors: list[str] = errors
        self.warnings: list[str] = warnings or []
        super().__init__("; ".join(errors))


class PluginValidator:
    """AST-based static validator for plugin files.

    Enforces naming conventions, manifest presence, forbidden patterns, and
    tool-name prefix compliance before any plugin code is executed.
    """

    def validate(self, plugin_path: Path) -> tuple[dict, list[str]]:
        """Validate *plugin_path* and return ``(manifest, warnings)``.

        Raises :class:`PluginValidationError` on any hard violation.
        """
        errors: list[str] = []
        warnings: list[str] = []

        plugin_label = plugin_path.name

        # ── 1. Parse the file ────────────────────────────────────────────────
        source = plugin_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(plugin_path))
        except SyntaxError as exc:
            logger.warning(
                "Validation layer 1 failed — syntax error",
                plugin=plugin_label,
                error=str(exc),
            )
            raise PluginValidationError(
                [f"Syntax error in {plugin_path.name}: {exc}"]
            ) from exc

        logger.debug("Validation layer 1 passed — file parsed", plugin=plugin_label)

        # ── 2. Locate PLUGIN_MANIFEST at module level ────────────────────────
        manifest_node: ast.Dict | None = None
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "PLUGIN_MANIFEST"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Dict)
            ):
                manifest_node = node.value
                break

        if manifest_node is None:
            logger.warning(
                "Validation layer 2 failed — PLUGIN_MANIFEST not found",
                plugin=plugin_label,
            )
            raise PluginValidationError(["PLUGIN_MANIFEST dict is required"])

        logger.debug("Validation layer 2 passed — PLUGIN_MANIFEST located", plugin=plugin_label)

        # ── 3. Extract manifest values from AST ──────────────────────────────
        manifest: dict = {}
        try:
            manifest = ast.literal_eval(manifest_node)
            if not isinstance(manifest, dict):
                logger.warning(
                    "Validation layer 3 failed — PLUGIN_MANIFEST not a dict",
                    plugin=plugin_label,
                )
                raise PluginValidationError(["PLUGIN_MANIFEST must be a dict literal"])
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Validation layer 3 failed — PLUGIN_MANIFEST non-literal values",
                plugin=plugin_label,
                error=str(exc),
            )
            raise PluginValidationError(
                [f"PLUGIN_MANIFEST must contain only literal values: {exc}"]
            ) from exc

        logger.debug("Validation layer 3 passed — manifest extracted", plugin=plugin_label)

        # ── 4. Required fields ───────────────────────────────────────────────
        for field in REQUIRED_MANIFEST_FIELDS:
            if field not in manifest:
                errors.append(f"PLUGIN_MANIFEST missing required field: '{field}'")

        if errors:
            logger.warning(
                "Validation layer 4 failed — missing required fields",
                plugin=plugin_label,
                missing_fields=[f for f in REQUIRED_MANIFEST_FIELDS if f not in manifest],
            )
            raise PluginValidationError(errors, warnings)

        logger.debug("Validation layer 4 passed — required fields present", plugin=plugin_label)

        name: str = str(manifest.get("name", ""))
        version: str = str(manifest.get("version", ""))
        tool_prefix: str = str(manifest.get("tool_prefix", ""))

        # ── 5. tool_prefix must not start with 'laravelgraph_' ───────────────
        if tool_prefix.startswith("laravelgraph_"):
            logger.warning(
                "Validation rule violated — reserved tool_prefix",
                plugin=plugin_label,
                tool_prefix=tool_prefix,
                rule="tool_prefix cannot start with 'laravelgraph_'",
            )
            errors.append(
                f"PLUGIN_MANIFEST tool_prefix '{tool_prefix}' cannot start with "
                f"the reserved 'laravelgraph_' prefix"
            )

        # ── 6. name: alphanumeric + hyphens only ─────────────────────────────
        if not _NAME_RE.match(name):
            logger.warning(
                "Validation rule violated — invalid plugin name",
                plugin=plugin_label,
                name=name,
                rule="name must be alphanumeric with hyphens only",
            )
            errors.append(
                f"PLUGIN_MANIFEST name '{name}' must be alphanumeric with hyphens only "
                f"(no spaces or special characters)"
            )

        # ── 7. version: semver-ish ──────────────────────────────────────────
        if not _SEMVER_RE.match(version):
            logger.warning(
                "Validation rule violated — invalid version format",
                plugin=plugin_label,
                version=version,
                rule="version must match MAJOR.MINOR.PATCH",
            )
            errors.append(
                f"PLUGIN_MANIFEST version '{version}' must match MAJOR.MINOR.PATCH "
                f"(e.g. '1.0.0')"
            )

        # ── 8. Forbidden AST patterns (errors) ───────────────────────────────
        for node in ast.walk(tree):
            # Banned network imports
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name
                        if module in ("requests", "httpx"):
                            logger.warning(
                                "Validation rule violated — banned import found",
                                plugin=plugin_label,
                                banned_import=module,
                                rule="network imports not allowed",
                            )
                            errors.append(
                                f"Forbidden import '{module}': network calls are not "
                                f"allowed in plugins"
                            )
                        elif module == "urllib.request" or module.startswith("urllib.request."):
                            logger.warning(
                                "Validation rule violated — banned import found",
                                plugin=plugin_label,
                                banned_import="urllib.request",
                                rule="network imports not allowed",
                            )
                            errors.append(
                                "Forbidden import 'urllib.request': network calls are not "
                                "allowed in plugins"
                            )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module in ("requests", "httpx"):
                        logger.warning(
                            "Validation rule violated — banned import found",
                            plugin=plugin_label,
                            banned_import=module,
                            rule="network imports not allowed",
                        )
                        errors.append(
                            f"Forbidden import '{module}': network calls are not "
                            f"allowed in plugins"
                        )
                    elif module == "urllib.request" or module.startswith("urllib.request"):
                        logger.warning(
                            "Validation rule violated — banned import found",
                            plugin=plugin_label,
                            banned_import="urllib.request",
                            rule="network imports not allowed",
                        )
                        errors.append(
                            "Forbidden import 'urllib.request': network calls are not "
                            "allowed in plugins"
                        )

            # Unsafe subprocess: os.system( or subprocess.Popen( without timeout
            if isinstance(node, ast.Call):
                call_str = ast.unparse(node)
                if "os.system(" in call_str:
                    logger.warning(
                        "Validation rule violated — unsafe subprocess call",
                        plugin=plugin_label,
                        call="os.system()",
                        rule="unsafe subprocess invocation not allowed",
                    )
                    errors.append(
                        f"Forbidden call 'os.system()': unsafe subprocess invocation"
                    )
                if "subprocess.Popen(" in call_str:
                    kwarg_names = {kw.arg for kw in node.keywords}
                    if "timeout" not in kwarg_names:
                        logger.warning(
                            "Validation rule violated — subprocess.Popen() without timeout",
                            plugin=plugin_label,
                            call="subprocess.Popen()",
                            rule="subprocess calls must include timeout=",
                        )
                        errors.append(
                            "Forbidden call 'subprocess.Popen()' without timeout= kwarg: "
                            "unsafe subprocess invocation"
                        )

            # Destructive Cypher / SQL in string literals
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val_upper = node.value.upper()
                if "DELETE " in val_upper and (
                    "NODE" in val_upper or "EDGE" in val_upper
                ):
                    logger.warning(
                        "Validation rule violated — destructive graph operation in string",
                        plugin=plugin_label,
                        pattern="DELETE node/edge",
                        rule="destructive graph operations not allowed",
                    )
                    errors.append(
                        f"Forbidden string pattern 'DELETE node/edge': "
                        f"destructive graph operations are not allowed"
                    )
                if "DROP TABLE" in val_upper:
                    logger.warning(
                        "Validation rule violated — destructive SQL in string",
                        plugin=plugin_label,
                        pattern="DROP TABLE",
                        rule="destructive database operations not allowed",
                    )
                    errors.append(
                        "Forbidden string pattern 'DROP TABLE': "
                        "destructive database operations are not allowed"
                    )
                if "TRUNCATE TABLE" in val_upper:
                    logger.warning(
                        "Validation rule violated — destructive SQL in string",
                        plugin=plugin_label,
                        pattern="TRUNCATE TABLE",
                        rule="destructive database operations not allowed",
                    )
                    errors.append(
                        "Forbidden string pattern 'TRUNCATE TABLE': "
                        "destructive database operations are not allowed"
                    )

        # ── 9. Warning patterns (collect, don't block) ───────────────────────
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                call_str = ast.unparse(node)

                # open() with write mode outside .laravelgraph/
                if call_str.startswith("open("):
                    for arg in node.args[1:2]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if "w" in arg.value or "wb" in arg.value:
                                warnings.append(
                                    f"open() call with write mode '{arg.value}' detected — "
                                    f"ensure writes target .laravelgraph/ only"
                                )
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                            mode = str(kw.value.value)
                            if "w" in mode:
                                warnings.append(
                                    f"open() call with write mode '{mode}' detected — "
                                    f"ensure writes target .laravelgraph/ only"
                                )

                # subprocess.run() without timeout=
                if "subprocess.run(" in call_str:
                    kwarg_names = {kw.arg for kw in node.keywords}
                    if "timeout" not in kwarg_names:
                        warnings.append(
                            "subprocess.run() called without timeout= kwarg — "
                            "consider adding a timeout to prevent hangs"
                        )

                # time.sleep() — performance concern
                if "time.sleep(" in call_str:
                    warnings.append(
                        "time.sleep() detected — sleeping inside a pipeline phase "
                        "degrades analysis performance"
                    )

        # ── 10. Tool names must use the declared tool_prefix ─────────────────
        if tool_prefix and not errors:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for decorator in node.decorator_list:
                        dec_str = ast.unparse(decorator)
                        if dec_str in ("mcp.tool()", "mcp.tool") or dec_str.startswith("mcp.tool("):
                            if not node.name.startswith(tool_prefix):
                                logger.warning(
                                    "Validation rule violated — wrong tool name prefix",
                                    plugin=plugin_label,
                                    tool_function=node.name,
                                    expected_prefix=tool_prefix,
                                    rule="tool names must start with declared tool_prefix",
                                )
                                errors.append(
                                    f"Tool function '{node.name}' is decorated with "
                                    f"@mcp.tool() but does not start with the declared "
                                    f"tool_prefix '{tool_prefix}'"
                                )

        if errors:
            raise PluginValidationError(errors, warnings)

        logger.debug(
            "All validation layers passed",
            plugin=plugin_label,
            name=name,
            version=version,
            warning_count=len(warnings),
        )
        return manifest, warnings


def validate_plugin(plugin_path: Path) -> tuple[dict, list[str]]:
    """Convenience wrapper around :class:`PluginValidator`.

    Returns ``(manifest, warnings)`` or raises :class:`PluginValidationError`.
    """
    return PluginValidator().validate(plugin_path)


def validate_plugin_file_content(code: str) -> _ValidatorResult:
    """Validate raw plugin source code (not a file path) through the static AST checks.

    This is the Layer 1 entry point used by the auto-generation system.  It runs
    all the same checks as :class:`PluginValidator` but accepts a code string
    directly rather than reading from disk.

    Returns a :class:`_ValidatorResult` with ``passed=True`` on success.
    Raises :class:`PluginValidationError` on hard violations (for caller
    compatibility with the reflection loop in generator.py, which also handles
    the result's ``.passed`` / ``.errors`` fields).
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ── 1. Parse the code string ─────────────────────────────────────────────
    try:
        tree = ast.parse(code, filename="<generated>")
    except SyntaxError as exc:
        raise PluginValidationError([f"Syntax error in generated code: {exc}"]) from exc

    # ── 2. Locate PLUGIN_MANIFEST at module level ─────────────────────────────
    manifest_node: ast.Dict | None = None
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "PLUGIN_MANIFEST"
                for t in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ):
            manifest_node = node.value
            break

    if manifest_node is None:
        raise PluginValidationError(["PLUGIN_MANIFEST dict is required"])

    # ── 3. Extract manifest values from AST ──────────────────────────────────
    manifest: dict = {}
    try:
        manifest = ast.literal_eval(manifest_node)
        if not isinstance(manifest, dict):
            raise PluginValidationError(["PLUGIN_MANIFEST must be a dict literal"])
    except (ValueError, TypeError) as exc:
        raise PluginValidationError(
            [f"PLUGIN_MANIFEST must contain only literal values: {exc}"]
        ) from exc

    # ── 4. Required fields ────────────────────────────────────────────────────
    for f_name in REQUIRED_MANIFEST_FIELDS:
        if f_name not in manifest:
            errors.append(f"PLUGIN_MANIFEST missing required field: '{f_name}'")

    if errors:
        raise PluginValidationError(errors, warnings)

    name: str = str(manifest.get("name", ""))
    version: str = str(manifest.get("version", ""))
    tool_prefix: str = str(manifest.get("tool_prefix", ""))

    # ── 5. tool_prefix must not start with 'laravelgraph_' ───────────────────
    if tool_prefix.startswith("laravelgraph_"):
        errors.append(
            f"PLUGIN_MANIFEST tool_prefix '{tool_prefix}' cannot start with "
            f"the reserved 'laravelgraph_' prefix"
        )

    # ── 6. name: alphanumeric + hyphens only ──────────────────────────────────
    if not _NAME_RE.match(name):
        errors.append(
            f"PLUGIN_MANIFEST name '{name}' must be alphanumeric with hyphens only "
            f"(no spaces or special characters)"
        )

    # ── 7. version: semver-ish ───────────────────────────────────────────────
    if not _SEMVER_RE.match(version):
        errors.append(
            f"PLUGIN_MANIFEST version '{version}' must match MAJOR.MINOR.PATCH "
            f"(e.g. '1.0.0')"
        )

    # ── 8. Forbidden AST patterns (errors) ───────────────────────────────────
    for node in ast.walk(tree):
        # Banned network imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if module in ("requests", "httpx"):
                        errors.append(
                            f"Forbidden import '{module}': network calls are not "
                            f"allowed in plugins"
                        )
                    elif module == "urllib.request" or module.startswith("urllib.request."):
                        errors.append(
                            "Forbidden import 'urllib.request': network calls are not "
                            "allowed in plugins"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in ("requests", "httpx"):
                    errors.append(
                        f"Forbidden import '{module}': network calls are not "
                        f"allowed in plugins"
                    )
                elif module == "urllib.request" or module.startswith("urllib.request"):
                    errors.append(
                        "Forbidden import 'urllib.request': network calls are not "
                        "allowed in plugins"
                    )

        # Unsafe subprocess: os.system( or subprocess.Popen( without timeout
        if isinstance(node, ast.Call):
            call_str = ast.unparse(node)
            if "os.system(" in call_str:
                errors.append(
                    "Forbidden call 'os.system()': unsafe subprocess invocation"
                )
            if "subprocess.Popen(" in call_str:
                kwarg_names = {kw.arg for kw in node.keywords}
                if "timeout" not in kwarg_names:
                    errors.append(
                        "Forbidden call 'subprocess.Popen()' without timeout= kwarg: "
                        "unsafe subprocess invocation"
                    )

        # Destructive Cypher / SQL in string literals
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val_upper = node.value.upper()
            if "DELETE " in val_upper and (
                "NODE" in val_upper or "EDGE" in val_upper
            ):
                errors.append(
                    "Forbidden string pattern 'DELETE node/edge': "
                    "destructive graph operations are not allowed"
                )
            if "DROP TABLE" in val_upper:
                errors.append(
                    "Forbidden string pattern 'DROP TABLE': "
                    "destructive database operations are not allowed"
                )
            if "TRUNCATE TABLE" in val_upper:
                errors.append(
                    "Forbidden string pattern 'TRUNCATE TABLE': "
                    "destructive database operations are not allowed"
                )

    # ── 9. Warning patterns (collect, don't block) ────────────────────────────
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_str = ast.unparse(node)

            # open() with write mode outside .laravelgraph/
            if call_str.startswith("open("):
                for arg in node.args[1:2]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if "w" in arg.value or "wb" in arg.value:
                            warnings.append(
                                f"open() call with write mode '{arg.value}' detected — "
                                f"ensure writes target .laravelgraph/ only"
                            )
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                        if "w" in mode:
                            warnings.append(
                                f"open() call with write mode '{mode}' detected — "
                                f"ensure writes target .laravelgraph/ only"
                            )

            # subprocess.run() without timeout=
            if "subprocess.run(" in call_str:
                kwarg_names = {kw.arg for kw in node.keywords}
                if "timeout" not in kwarg_names:
                    warnings.append(
                        "subprocess.run() called without timeout= kwarg — "
                        "consider adding a timeout to prevent hangs"
                    )

            # time.sleep() — performance concern
            if "time.sleep(" in call_str:
                warnings.append(
                    "time.sleep() detected — sleeping inside a pipeline phase "
                    "degrades analysis performance"
                )

    # ── 10. Tool names must use the declared tool_prefix ─────────────────────
    if tool_prefix and not errors:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    dec_str = ast.unparse(decorator)
                    if dec_str in ("mcp.tool()", "mcp.tool") or dec_str.startswith("mcp.tool("):
                        if not node.name.startswith(tool_prefix):
                            errors.append(
                                f"Tool function '{node.name}' is decorated with "
                                f"@mcp.tool() but does not start with the declared "
                                f"tool_prefix '{tool_prefix}'"
                            )

    if errors:
        raise PluginValidationError(errors, warnings)

    return _ValidatorResult(passed=True, errors=[], warnings=warnings)
