"""Tests for the PHP parser against the tiny-laravel-app fixture files."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from laravelgraph.parsers.php import PHPParser, PHPFile

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def parser() -> PHPParser:
    return PHPParser()


@pytest.fixture(scope="module")
def user_php(parser: PHPParser) -> PHPFile:
    return parser.parse_file(TINY_APP / "app" / "Models" / "User.php")


@pytest.fixture(scope="module")
def user_controller_php(parser: PHPParser) -> PHPFile:
    return parser.parse_file(TINY_APP / "app" / "Http" / "Controllers" / "UserController.php")


@pytest.fixture(scope="module")
def user_service_php(parser: PHPParser) -> PHPFile:
    return parser.parse_file(TINY_APP / "app" / "Services" / "UserService.php")


@pytest.fixture(scope="module")
def middleware_php(parser: PHPParser) -> PHPFile:
    return parser.parse_file(TINY_APP / "app" / "Http" / "Middleware" / "AuthenticateApi.php")


# ── Basic class parsing ───────────────────────────────────────────────────────

class TestParseUserModel:
    def test_parse_user_model_no_errors(self, user_php: PHPFile):
        """Parsing User.php should succeed without fatal errors."""
        assert len(user_php.classes) == 1

    def test_class_name(self, user_php: PHPFile):
        cls = user_php.classes[0]
        assert cls.name == "User"

    def test_class_fqn(self, user_php: PHPFile):
        cls = user_php.classes[0]
        assert cls.fqn == "App\\Models\\User"

    def test_methods_present(self, user_php: PHPFile):
        method_names = {m.name for m in user_php.classes[0].methods}
        assert "posts" in method_names
        assert "profile" in method_names
        assert "scopeActive" in method_names
        assert "getFullNameAttribute" in method_names

    def test_method_count(self, user_php: PHPFile):
        # User has exactly 4 methods: posts, profile, scopeActive, getFullNameAttribute
        assert len(user_php.classes[0].methods) == 4


class TestParseNamespace:
    def test_namespace_extracted(self, user_php: PHPFile):
        assert user_php.namespace == "App\\Models"

    def test_controller_namespace(self, user_controller_php: PHPFile):
        assert user_controller_php.namespace == "App\\Http\\Controllers"

    def test_service_namespace(self, user_service_php: PHPFile):
        assert user_service_php.namespace == "App\\Services"


class TestParseUseStatements:
    def test_use_statements_parsed(self, user_php: PHPFile):
        use_fqns = {u.fqn for u in user_php.uses}
        assert "Illuminate\\Database\\Eloquent\\Model" in use_fqns
        assert "Illuminate\\Database\\Eloquent\\SoftDeletes" in use_fqns
        assert "App\\Models\\Post" in use_fqns
        assert "App\\Models\\Profile" in use_fqns

    def test_use_aliases_derived(self, user_php: PHPFile):
        aliases = {u.alias for u in user_php.uses}
        assert "Model" in aliases
        assert "SoftDeletes" in aliases

    def test_controller_uses(self, user_controller_php: PHPFile):
        use_fqns = {u.fqn for u in user_controller_php.uses}
        assert "App\\Models\\User" in use_fqns
        assert "App\\Services\\UserService" in use_fqns


class TestParseTraits:
    def test_softdeletes_trait_detected(self, user_php: PHPFile):
        cls = user_php.classes[0]
        assert "SoftDeletes" in cls.traits

    def test_post_has_no_traits(self):
        parser = PHPParser()
        post_php = parser.parse_file(TINY_APP / "app" / "Models" / "Post.php")
        cls = post_php.classes[0]
        assert cls.traits == []


# ── Eloquent relationship parsing ─────────────────────────────────────────────

class TestParseRelationships:
    def test_has_many_detected(self, user_php: PHPFile):
        cls = user_php.classes[0]
        posts_method = next(m for m in cls.methods if m.name == "posts")
        # Verify the method has calls (hasMany call)
        assert any(c.method == "hasMany" for c in posts_method.calls), (
            f"Expected hasMany call, found: {[c.method for c in posts_method.calls]}"
        )

    def test_has_one_detected(self, user_php: PHPFile):
        cls = user_php.classes[0]
        profile_method = next(m for m in cls.methods if m.name == "profile")
        assert any(c.method == "hasOne" for c in profile_method.calls), (
            f"Expected hasOne call, found: {[c.method for c in profile_method.calls]}"
        )

    def test_belongs_to_detected(self):
        parser = PHPParser()
        post_php = parser.parse_file(TINY_APP / "app" / "Models" / "Post.php")
        cls = post_php.classes[0]
        user_method = next(m for m in cls.methods if m.name == "user")
        assert any(c.method == "belongsTo" for c in user_method.calls), (
            f"Expected belongsTo call, found: {[c.method for c in user_method.calls]}"
        )

    def test_belongs_to_many_detected(self):
        parser = PHPParser()
        post_php = parser.parse_file(TINY_APP / "app" / "Models" / "Post.php")
        cls = post_php.classes[0]
        tags_method = next(m for m in cls.methods if m.name == "tags")
        assert any(c.method == "belongsToMany" for c in tags_method.calls), (
            f"Expected belongsToMany call, found: {[c.method for c in tags_method.calls]}"
        )


# ── Controller parsing ────────────────────────────────────────────────────────

class TestParseController:
    def test_controller_methods(self, user_controller_php: PHPFile):
        method_names = {m.name for m in user_controller_php.classes[0].methods}
        assert "index" in method_names
        assert "store" in method_names
        assert "show" in method_names
        assert "destroy" in method_names

    def test_controller_class_name(self, user_controller_php: PHPFile):
        assert user_controller_php.classes[0].name == "UserController"

    def test_controller_fqn(self, user_controller_php: PHPFile):
        assert user_controller_php.classes[0].fqn == "App\\Http\\Controllers\\UserController"


# ── Call extraction ───────────────────────────────────────────────────────────

class TestCallExtraction:
    def test_hash_make_call_extracted(self, user_service_php: PHPFile):
        cls = user_service_php.classes[0]
        create_method = next(m for m in cls.methods if m.name == "create")
        all_calls = [(c.receiver, c.method) for c in create_method.calls]
        assert ("Hash", "make") in all_calls, f"Expected Hash::make, found: {all_calls}"

    def test_this_calls_extracted(self, user_php: PHPFile):
        cls = user_php.classes[0]
        posts_method = next(m for m in cls.methods if m.name == "posts")
        # $this->hasMany(...) should be captured
        assert len(posts_method.calls) >= 1

    def test_instance_call_is_not_static(self, user_php: PHPFile):
        cls = user_php.classes[0]
        posts_method = next(m for m in cls.methods if m.name == "posts")
        has_many_call = next((c for c in posts_method.calls if c.method == "hasMany"), None)
        assert has_many_call is not None
        assert has_many_call.is_static is False

    def test_static_call_is_static(self, user_service_php: PHPFile):
        cls = user_service_php.classes[0]
        create_method = next(m for m in cls.methods if m.name == "create")
        hash_call = next((c for c in create_method.calls if c.method == "make"), None)
        assert hash_call is not None
        assert hash_call.is_static is True


# ── Facade detection ──────────────────────────────────────────────────────────

class TestFacadeDetection:
    def test_auth_check_is_facade_call(self, middleware_php: PHPFile):
        cls = middleware_php.classes[0]
        handle_method = next(m for m in cls.methods if m.name == "handle")
        auth_calls = [c for c in handle_method.calls if c.receiver == "Auth"]
        assert len(auth_calls) >= 1, f"Expected Auth facade call, found: {handle_method.calls}"
        assert any(c.method == "check" for c in auth_calls)

    def test_hash_facade_in_service(self, user_service_php: PHPFile):
        cls = user_service_php.classes[0]
        create_method = next(m for m in cls.methods if m.name == "create")
        hash_calls = [c for c in create_method.calls if c.receiver == "Hash"]
        assert len(hash_calls) >= 1


# ── Abstract and final classes ────────────────────────────────────────────────

class TestParseAbstractClass:
    def test_abstract_class_parsing(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Base;

abstract class BaseRepository
{
    abstract public function find(int $id): ?object;

    public function all(): array
    {
        return [];
    }
}
"""
        php_file = tmp_path / "BaseRepository.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.classes) == 1
        cls = result.classes[0]
        assert cls.name == "BaseRepository"
        assert cls.is_abstract is True
        assert cls.is_final is False

    def test_final_class_parsing(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Services;

final class CacheService
{
    public function get(string $key): mixed
    {
        return null;
    }
}
"""
        php_file = tmp_path / "CacheService.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.classes) == 1
        cls = result.classes[0]
        assert cls.is_final is True
        assert cls.is_abstract is False


# ── Interface parsing ─────────────────────────────────────────────────────────

class TestParseInterface:
    def test_interface_parsed(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Contracts;

interface UserRepositoryInterface
{
    public function find(int $id): ?object;
    public function create(array $data): object;
    public function delete(int $id): bool;
}
"""
        php_file = tmp_path / "UserRepositoryInterface.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.interfaces) == 1
        iface = result.interfaces[0]
        assert iface.name == "UserRepositoryInterface"
        assert iface.fqn == "App\\Contracts\\UserRepositoryInterface"

    def test_interface_methods_parsed(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Contracts;

interface CacheInterface
{
    public function get(string $key): mixed;
    public function set(string $key, mixed $value, int $ttl = 3600): void;
    public function forget(string $key): void;
}
"""
        php_file = tmp_path / "CacheInterface.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.interfaces) == 1
        methods = {m.name for m in result.interfaces[0].methods}
        assert "get" in methods
        assert "set" in methods
        assert "forget" in methods

    def test_interface_extends_parsed(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Contracts;

interface ExtendedCacheInterface extends CacheInterface
{
    public function tags(array $tags): static;
}
"""
        php_file = tmp_path / "ExtendedCacheInterface.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.interfaces) == 1
        iface = result.interfaces[0]
        assert "CacheInterface" in iface.extends


# ── Enum parsing ──────────────────────────────────────────────────────────────

class TestParseEnum:
    def test_string_backed_enum(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Enums;

enum UserStatus: string
{
    case Active = 'active';
    case Inactive = 'inactive';
    case Suspended = 'suspended';
}
"""
        php_file = tmp_path / "UserStatus.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.enums) == 1
        enum = result.enums[0]
        assert enum.name == "UserStatus"
        assert enum.fqn == "App\\Enums\\UserStatus"
        assert enum.backed_type == "string"
        assert "Active" in enum.cases
        assert "Inactive" in enum.cases
        assert "Suspended" in enum.cases

    def test_int_backed_enum(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Enums;

enum Priority: int
{
    case Low = 1;
    case Medium = 2;
    case High = 3;
}
"""
        php_file = tmp_path / "Priority.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.enums) == 1
        assert result.enums[0].backed_type == "int"

    def test_pure_enum(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Enums;

enum Direction
{
    case North;
    case South;
    case East;
    case West;
}
"""
        php_file = tmp_path / "Direction.php"
        php_file.write_bytes(php_content)
        parser = PHPParser()
        result = parser.parse_file(php_file)
        assert len(result.enums) == 1
        assert result.enums[0].backed_type == ""


# ── Regex fallback ────────────────────────────────────────────────────────────

class TestRegexFallback:
    def test_regex_fallback_parses_classes(self, tmp_path: Path):
        """Verify regex fallback produces valid results when tree-sitter is unavailable."""
        php_content = b"""<?php
namespace App\\Services;

class PaymentService
{
    public function charge(float $amount): bool
    {
        return true;
    }

    public function refund(float $amount): bool
    {
        return false;
    }
}
"""
        php_file = tmp_path / "PaymentService.php"
        php_file.write_bytes(php_content)

        parser = PHPParser()
        # Force regex fallback by patching _get_parser to return False
        with patch("laravelgraph.parsers.php._get_parser", return_value=False):
            result = parser.parse_file(php_file)

        assert len(result.classes) == 1
        cls = result.classes[0]
        assert cls.name == "PaymentService"
        assert cls.fqn == "App\\Services\\PaymentService"

    def test_regex_fallback_extracts_namespace(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Domain\\Billing;

class Invoice
{
    public function total(): float { return 0.0; }
}
"""
        php_file = tmp_path / "Invoice.php"
        php_file.write_bytes(php_content)

        parser = PHPParser()
        with patch("laravelgraph.parsers.php._get_parser", return_value=False):
            result = parser.parse_file(php_file)

        assert result.namespace == "App\\Domain\\Billing"

    def test_regex_fallback_extracts_methods(self, tmp_path: Path):
        php_content = b"""<?php
namespace App\\Services;

class OrderService
{
    public function placeOrder(array $items): object
    {
        return (object)[];
    }

    protected function validateItems(array $items): bool
    {
        return true;
    }

    private function calculateTax(float $subtotal): float
    {
        return $subtotal * 0.1;
    }
}
"""
        php_file = tmp_path / "OrderService.php"
        php_file.write_bytes(php_content)

        parser = PHPParser()
        with patch("laravelgraph.parsers.php._get_parser", return_value=False):
            result = parser.parse_file(php_file)

        assert len(result.classes) == 1
        method_names = {m.name for m in result.classes[0].methods}
        assert "placeOrder" in method_names
        assert "validateItems" in method_names
        assert "calculateTax" in method_names
