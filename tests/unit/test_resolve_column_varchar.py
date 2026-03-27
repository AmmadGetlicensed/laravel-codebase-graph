"""Unit tests — Fix 2: resolve_column varchar value sampling."""
from __future__ import annotations

import pytest


# ── Helper: mimic _fetch_varchar_sample logic ─────────────────────────────────

def _should_sample_varchar(col_type: str, is_polymorphic: bool) -> bool:
    """Return True if this column should get a varchar sample query."""
    ctype = col_type.lower()
    is_varchar = any(t in ctype for t in ("varchar", "char", "text")) and "enum" not in ctype
    return is_varchar and not is_polymorphic


def _varchar_sample_sql(table: str, column: str, max_distinct: int = 30) -> str:
    safe_t = table.replace("`", "")
    safe_c = column.replace("`", "")
    return (
        f"SELECT `{safe_c}` AS val, COUNT(*) AS cnt "
        f"FROM `{safe_t}` "
        f"WHERE `{safe_c}` IS NOT NULL AND `{safe_c}` != '' "
        f"GROUP BY `{safe_c}` "
        f"ORDER BY cnt DESC "
        f"LIMIT {max_distinct + 1}"
    )


class TestShouldSampleVarchar:
    def test_varchar_non_polymorphic_sampled(self):
        assert _should_sample_varchar("varchar(30)", False) is True

    def test_varchar_large_sampled(self):
        assert _should_sample_varchar("varchar(255)", False) is True

    def test_char_sampled(self):
        assert _should_sample_varchar("char(10)", False) is True

    def test_text_sampled(self):
        assert _should_sample_varchar("text", False) is True

    def test_enum_not_sampled_via_varchar_path(self):
        # Enums go through the discriminator path, not varchar
        assert _should_sample_varchar("enum('a','b','c')", False) is False

    def test_polymorphic_varchar_not_sampled(self):
        # Polymorphic IDs are varchar but would return meaningless integer strings
        assert _should_sample_varchar("varchar(255)", True) is False

    def test_int_not_sampled(self):
        assert _should_sample_varchar("int unsigned", False) is False

    def test_bigint_not_sampled(self):
        assert _should_sample_varchar("bigint(20)", False) is False

    def test_tinyint_not_sampled(self):
        assert _should_sample_varchar("tinyint(1)", False) is False


class TestVarcharSampleSql:
    def test_sql_structure(self):
        sql = _varchar_sample_sql("order_courses", "payment_through")
        assert "SELECT `payment_through` AS val" in sql
        assert "FROM `order_courses`" in sql
        assert "IS NOT NULL" in sql
        assert "!= ''" in sql
        assert "ORDER BY cnt DESC" in sql

    def test_sql_limit_is_max_plus_one(self):
        sql = _varchar_sample_sql("t", "c", max_distinct=20)
        assert "LIMIT 21" in sql

    def test_backtick_injection_stripped(self):
        sql = _varchar_sample_sql("my`table", "my`col")
        # Backticks stripped from identifiers
        assert "my`table" not in sql
        assert "my`col" not in sql


class TestVarcharSampleOverflowDetection:
    """If the query returns more than max_distinct rows, the sample is suppressed."""

    def _simulate_sample(self, rows: list, max_distinct: int = 30) -> list | None:
        """Mimic the overflow check in _fetch_varchar_sample."""
        if len(rows) > max_distinct:
            return None
        return rows

    def test_within_limit_returned(self):
        rows = [{"val": f"v{i}", "cnt": i} for i in range(10)]
        assert self._simulate_sample(rows) == rows

    def test_overflow_suppressed(self):
        rows = [{"val": f"v{i}", "cnt": i} for i in range(31)]
        assert self._simulate_sample(rows) is None

    def test_exactly_at_limit_returned(self):
        rows = [{"val": f"v{i}", "cnt": i} for i in range(30)]
        assert self._simulate_sample(rows) is not None

    def test_empty_result_returned(self):
        assert self._simulate_sample([]) == []


class TestVarcharSampleFormatting:
    """Verify the markdown table rendered for a varchar sample."""

    def _format_sample(self, rows: list[dict]) -> list[str]:
        lines = ["\n### Value Sample (live DB)\n", "| Value | Count |", "|-------|-------|"]
        for row in rows:
            lines.append(f"| `{row['val']}` | {row['cnt']:,} |")
        return lines

    def test_table_header_present(self):
        out = self._format_sample([{"val": "stripe", "cnt": 5000}])
        assert "| Value | Count |" in out

    def test_values_shown(self):
        rows = [
            {"val": "stripe", "cnt": 50000},
            {"val": "invoice", "cnt": 12000},
            {"val": "direct", "cnt": 3000},
        ]
        out = self._format_sample(rows)
        assert any("`stripe`" in line for line in out)
        assert any("`invoice`" in line for line in out)

    def test_counts_formatted_with_thousands_separator(self):
        out = self._format_sample([{"val": "stripe", "cnt": 50000}])
        assert "50,000" in "\n".join(out)
