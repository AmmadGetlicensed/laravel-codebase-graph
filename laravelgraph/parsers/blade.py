"""Blade template parser.

Extracts:
- @extends, @include, @includeIf, @includeWhen, @includeFirst
- @component, <x-component> tags
- @livewire, @livewireStyles, @livewireScripts
- @section, @yield, @push, @stack, @slot
- @props declarations
- Variables referenced ({{ $var }}, {!! $var !!})
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BladeDirective:
    name: str
    args: str
    line: int


@dataclass
class BladeComponent:
    tag: str        # "x-alert" or component name from @component
    props: dict[str, str]
    line: int
    is_x_tag: bool  # True for <x-...>, False for @component


@dataclass
class BladeParsed:
    path: str
    view_name: str   # dot notation (e.g. "layouts.app")
    extends: str | None
    sections: list[str]
    yields: list[str]
    stacks: list[str]
    pushes: list[str]
    includes: list[str]          # view names included
    components: list[BladeComponent]
    livewire_components: list[str]
    props: list[str]
    slots: list[str]
    variables: list[str]
    directives: list[BladeDirective]
    errors: list[str] = field(default_factory=list)


# ── Regex patterns ──────────────────────────────────────────────────────────

_EXTENDS = re.compile(r"@extends\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_SECTION = re.compile(r"@section\s*\(\s*['\"]([^'\"]+)['\"]")
_YIELD = re.compile(r"@yield\s*\(\s*['\"]([^'\"]+)['\"]")
_STACK = re.compile(r"@stack\s*\(\s*['\"]([^'\"]+)['\"]")
_PUSH = re.compile(r"@(?:push|pushIf|pushOnce|prepend)\s*\(\s*['\"]([^'\"]+)['\"]")
_INCLUDE = re.compile(
    r"@(?:include|includeIf|includeWhen|includeUnless|includeFirst|each)\s*\(\s*['\"]([^'\"]+)['\"]"
)
_COMPONENT = re.compile(r"@component\s*\(\s*['\"]([^'\"]+)['\"]")
_LIVEWIRE = re.compile(r"@(?:livewire|Livewire::mount)\s*\(\s*['\"]([^'\"]+)['\"]")
_LIVEWIRE_TAG = re.compile(r"<livewire:([a-z0-9\-\.]+)")
_X_TAG_OPEN = re.compile(r"<x-([a-z0-9\-\.:]+)(\s[^>]*)?>", re.IGNORECASE)
_PROPS = re.compile(r"@props\s*\(\s*(\[[^\]]+\]|\{[^}]+\})")
_SLOT = re.compile(r"@slot\s*\(\s*['\"]([^'\"]+)['\"]|<x-slot[:\s]name=['\"]([^'\"]+)['\"]")
_VARIABLE = re.compile(r"\{\{-?\s*\$(\w+)|{!!\s*\$(\w+)")
_DIRECTIVE = re.compile(r"@(\w+)(?:\s*\(([^)]*)\))?")
_PROP_KEY = re.compile(r"['\"]([^'\"]+)['\"]")


class BladeParser:
    """Parses Blade template files into structured metadata."""

    def parse_file(self, path: Path, views_root: Path | None = None) -> BladeParsed:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return BladeParsed(
                path=str(path), view_name="", extends=None,
                sections=[], yields=[], stacks=[], pushes=[],
                includes=[], components=[], livewire_components=[],
                props=[], slots=[], variables=[], directives=[],
                errors=[str(e)],
            )

        view_name = self._derive_view_name(path, views_root)
        return self._parse_source(str(path), view_name, source)

    @staticmethod
    def _derive_view_name(path: Path, views_root: Path | None) -> str:
        """Convert file path to dot-notation view name."""
        if views_root:
            try:
                rel = path.relative_to(views_root)
                # Strip .blade.php
                parts = list(rel.parts)
                if parts:
                    parts[-1] = re.sub(r"\.blade\.php$", "", parts[-1])
                return ".".join(parts)
            except ValueError:
                pass
        return path.stem.replace(".blade", "")

    def _parse_source(self, path: str, view_name: str, source: str) -> BladeParsed:
        lines = source.splitlines()

        extends: str | None = None
        sections: list[str] = []
        yields: list[str] = []
        stacks: list[str] = []
        pushes: list[str] = []
        includes: list[str] = []
        components: list[BladeComponent] = []
        livewire_components: list[str] = []
        props: list[str] = []
        slots: list[str] = []
        variables: list[str] = []
        directives: list[BladeDirective] = []

        seen_vars: set[str] = set()

        for i, line in enumerate(lines, 1):
            # @extends
            if m := _EXTENDS.search(line):
                extends = m.group(1)

            # @section
            for m in _SECTION.finditer(line):
                name = m.group(1)
                if name not in sections:
                    sections.append(name)

            # @yield
            for m in _YIELD.finditer(line):
                name = m.group(1)
                if name not in yields:
                    yields.append(name)

            # @stack
            for m in _STACK.finditer(line):
                name = m.group(1)
                if name not in stacks:
                    stacks.append(name)

            # @push / @prepend
            for m in _PUSH.finditer(line):
                name = m.group(1)
                if name not in pushes:
                    pushes.append(name)

            # @include variants
            for m in _INCLUDE.finditer(line):
                view = m.group(1)
                if view not in includes:
                    includes.append(view)

            # @component(...)
            if m := _COMPONENT.search(line):
                name = m.group(1)
                components.append(BladeComponent(
                    tag=name, props={}, line=i, is_x_tag=False,
                ))

            # <x-component-name ...>
            for m in _X_TAG_OPEN.finditer(line):
                tag = m.group(1)
                attr_str = m.group(2) or ""
                attr_props = self._parse_attrs(attr_str)
                components.append(BladeComponent(
                    tag=tag, props=attr_props, line=i, is_x_tag=True,
                ))

            # @livewire / <livewire:...>
            if m := _LIVEWIRE.search(line):
                name = m.group(1)
                if name not in livewire_components:
                    livewire_components.append(name)
            for m in _LIVEWIRE_TAG.finditer(line):
                name = m.group(1)
                if name not in livewire_components:
                    livewire_components.append(name)

            # @props
            if m := _PROPS.search(line):
                raw = m.group(1)
                for key_m in _PROP_KEY.finditer(raw):
                    key = key_m.group(1)
                    if key not in props:
                        props.append(key)

            # @slot
            for m in _SLOT.finditer(line):
                name = m.group(1) or m.group(2)
                if name and name not in slots:
                    slots.append(name)

            # Variables
            for m in _VARIABLE.finditer(line):
                var = m.group(1) or m.group(2)
                if var and var not in seen_vars:
                    seen_vars.add(var)
                    variables.append(var)

            # Generic directives (for logging/completeness)
            for m in _DIRECTIVE.finditer(line):
                dir_name = m.group(1)
                if dir_name not in {
                    "extends", "section", "endsection", "yield", "stack", "push",
                    "endpush", "include", "component", "endcomponent", "livewire",
                    "props", "slot", "endslot", "if", "else", "elseif", "endif",
                    "foreach", "endforeach", "for", "endfor", "while", "endwhile",
                    "php", "endphp", "verbatim", "endverbatim",
                }:
                    directives.append(BladeDirective(
                        name=dir_name, args=(m.group(2) or "").strip(), line=i,
                    ))

        return BladeParsed(
            path=path, view_name=view_name, extends=extends,
            sections=sections, yields=yields, stacks=stacks, pushes=pushes,
            includes=includes, components=components,
            livewire_components=livewire_components,
            props=props, slots=slots, variables=list(variables),
            directives=directives,
        )

    @staticmethod
    def _parse_attrs(attr_str: str) -> dict[str, str]:
        """Very basic HTML attribute parser for x-component attributes."""
        attrs = {}
        for m in re.finditer(r'(\:?[\w\-]+)\s*=\s*["\']([^"\']*)["\']', attr_str):
            attrs[m.group(1)] = m.group(2)
        return attrs
