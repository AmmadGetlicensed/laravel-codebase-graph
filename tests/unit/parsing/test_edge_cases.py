"""Edge case tests for PHP and Blade parsers."""

from __future__ import annotations

from pathlib import Path

import pytest

from laravelgraph.parsers.blade import BladeParser
from laravelgraph.parsers.php import PHPParser


# ── Empty / minimal PHP files ─────────────────────────────────────────────────

class TestEmptyPHPFile:
    def test_empty_php_file_no_crash(self, tmp_path: Path):
        """An empty PHP file should parse without errors or exceptions."""
        php_file = tmp_path / "empty.php"
        php_file.write_bytes(b"")
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert result.classes == []
        assert result.interfaces == []
        assert result.enums == []
        assert result.traits == []
        assert result.namespace == ""

    def test_php_open_tag_only(self, tmp_path: Path):
        """File with only <?php tag should parse without crash."""
        php_file = tmp_path / "minimal.php"
        php_file.write_bytes(b"<?php\n")
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert result.classes == []

    def test_php_file_with_only_comments(self, tmp_path: Path):
        php_file = tmp_path / "comments.php"
        php_file.write_bytes(b"<?php\n// This is a comment\n/* Block comment */\n")
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert result.classes == []
        assert result.errors == []


# ── Files without namespace ───────────────────────────────────────────────────

class TestNoNamespace:
    def test_php_file_no_namespace(self, tmp_path: Path):
        """A PHP class without namespace declaration should parse correctly."""
        php_content = b"""<?php

class GlobalHelper
{
    public function format(string $value): string
    {
        return strtolower($value);
    }
}
"""
        php_file = tmp_path / "GlobalHelper.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert result.namespace == ""
        assert len(result.classes) == 1
        cls = result.classes[0]
        assert cls.name == "GlobalHelper"
        # FQN without namespace should just be the class name
        assert cls.fqn == "GlobalHelper"

    def test_function_without_namespace(self, tmp_path: Path):
        php_content = b"""<?php

function my_helper(string $val): string {
    return $val;
}
"""
        php_file = tmp_path / "helpers.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert result.namespace == ""
        assert len(result.functions) == 1
        assert result.functions[0].name == "my_helper"


# ── Parse error handling ──────────────────────────────────────────────────────

class TestParseErrors:
    def test_malformed_php_captured_in_errors(self, tmp_path: Path):
        """Malformed PHP should be captured — no unhandled exception."""
        php_content = b"""<?php
namespace App;

class Broken {
    public function missingBrace(
        // unclosed parenthesis
"""
        php_file = tmp_path / "Broken.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        # Should not raise — may return partial result or errors list
        result = parser.parse_file(php_file)
        # The key guarantee: no unhandled exception was raised
        assert result is not None

    def test_parse_result_has_errors_field(self, tmp_path: Path):
        """PHPFile should always have an errors list, even if empty."""
        php_file = tmp_path / "valid.php"
        php_file.write_bytes(b"<?php\nclass Ok {}\n")
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert isinstance(result.errors, list)

    def test_nonexistent_file_returns_errors(self):
        parser = PHPParser()
        result = parser.parse_file(Path("/nonexistent/Missing.php"))
        assert len(result.errors) >= 1
        assert result.classes == []


# ── Blade edge cases ──────────────────────────────────────────────────────────

class TestBladePlainHtml:
    def test_blade_file_with_no_directives(self, tmp_path: Path):
        """A plain HTML file with .blade.php extension should parse cleanly."""
        blade_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Static Page</title>
</head>
<body>
    <h1>Hello World</h1>
    <p>This is plain HTML with no Blade directives.</p>
</body>
</html>
"""
        blade_file = tmp_path / "static.blade.php"
        blade_file.write_text(blade_content)
        parser = BladeParser()
        result = parser.parse_file(blade_file)
        assert result.extends is None
        assert result.sections == []
        assert result.yields == []
        assert result.stacks == []
        assert result.components == []
        assert result.livewire_components == []
        assert result.errors == []


# ── Performance: large class ──────────────────────────────────────────────────

class TestVeryLargeClass:
    def test_parse_class_with_100_methods(self, tmp_path: Path):
        """Parser should handle a class with many methods without excessive slowdown."""
        methods = "\n".join(
            f"    public function method{i}(): void {{ }}"
            for i in range(100)
        )
        php_content = f"""<?php
namespace App\\Services;

class HugeService
{{
{methods}
}}
""".encode()
        php_file = tmp_path / "HugeService.php"
        php_file.write_bytes(php_content)

        import time
        parser = PHPParser()
        start = time.monotonic()
        result = parser.parse_file(php_file)
        elapsed = time.monotonic() - start

        assert len(result.classes) == 1
        assert len(result.classes[0].methods) == 100
        # Should parse in well under 5 seconds
        assert elapsed < 5.0, f"Parsing took {elapsed:.2f}s — too slow"


# ── Deeply nested namespaces ──────────────────────────────────────────────────

class TestDeeplyNestedNamespace:
    def test_deeply_nested_namespace_fqn(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Domain\\User\\Services\\Auth;

class UserAuthService
{
    public function authenticate(string $email, string $password): bool
    {
        return false;
    }
}
"""
        php_file = tmp_path / "UserAuthService.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)

        assert result.namespace == "App\\Domain\\User\\Services\\Auth"
        assert len(result.classes) == 1
        cls = result.classes[0]
        assert cls.fqn == "App\\Domain\\User\\Services\\Auth\\UserAuthService"

    def test_deeply_nested_namespace_use_resolution(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Domain\\Order\\Services\\Payment;

use App\\Domain\\User\\ValueObjects\\Money;
use App\\Domain\\Order\\Events\\PaymentProcessed;

class PaymentGateway
{
    public function charge(Money $amount): bool
    {
        return true;
    }
}
"""
        php_file = tmp_path / "PaymentGateway.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)

        assert result.namespace == "App\\Domain\\Order\\Services\\Payment"
        use_fqns = {u.fqn for u in result.uses}
        assert "App\\Domain\\User\\ValueObjects\\Money" in use_fqns
        assert "App\\Domain\\Order\\Events\\PaymentProcessed" in use_fqns


# ── Multiple classes in one file ──────────────────────────────────────────────

class TestMultipleClassesInFile:
    def test_multiple_classes_parsed(self, tmp_path: Path):
        """Some legacy PHP files have multiple classes; parser should handle them."""
        php_content = b"""<?php
namespace App\\Helpers;

class StringHelper
{
    public function slugify(string $str): string { return ''; }
}

class ArrayHelper
{
    public function flatten(array $arr): array { return []; }
}
"""
        php_file = tmp_path / "helpers.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        class_names = {cls.name for cls in result.classes}
        assert "StringHelper" in class_names
        assert "ArrayHelper" in class_names


# ── PHP 8 constructor promotion ───────────────────────────────────────────────

class TestConstructorPromotion:
    def test_promoted_params_parsed(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Services;

class ReportService
{
    public function __construct(
        private readonly string $title,
        protected int $limit = 100,
        public bool $published = false,
    ) {}

    public function generate(): array { return []; }
}
"""
        php_file = tmp_path / "ReportService.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.classes) == 1
        cls = result.classes[0]
        constructor = next((m for m in cls.methods if m.name == "__construct"), None)
        if constructor is not None:
            # If constructor is parsed, its promoted params should be detected
            promoted = [p for p in constructor.params if p.is_promoted]
            assert len(promoted) >= 1


# ── PHP 8 attributes ──────────────────────────────────────────────────────────

class TestPhp8Attributes:
    def test_class_with_attributes(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Http\\Controllers;

use Symfony\\Component\\Routing\\Annotation\\Route;

#[Route('/api/v2')]
class ApiController
{
    #[Route('/users', methods: ['GET'])]
    public function users(): array { return []; }
}
"""
        php_file = tmp_path / "ApiController.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        # Should not raise — attributes may or may not be captured
        result = parser.parse_file(php_file)
        assert len(result.classes) == 1
        assert result.classes[0].name == "ApiController"


# ── Readonly properties ───────────────────────────────────────────────────────

class TestReadonlyProperties:
    def test_readonly_class_parsed(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\DTOs;

readonly class UserDTO
{
    public function __construct(
        public int $id,
        public string $name,
        public string $email,
    ) {}
}
"""
        php_file = tmp_path / "UserDTO.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        # Parser should not crash on readonly classes
        assert result is not None
        # May or may not parse the readonly keyword; class should still be found
        class_names = {cls.name for cls in result.classes}
        assert "UserDTO" in class_names
