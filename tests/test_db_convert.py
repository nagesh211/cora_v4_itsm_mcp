"""Tests for the psycopg %s -> asyncpg $n placeholder converter."""
import datetime

from cora_mcp.db import _coerce_for_pgtype, _coerce_temporal, resolve_dsn, to_asyncpg


def test_coerce_for_pgtype_bool():
    assert _coerce_for_pgtype("1", "bool") is True
    assert _coerce_for_pgtype("true", "boolean") is True
    assert _coerce_for_pgtype("0", "bool") is False
    assert _coerce_for_pgtype("f", "bool") is False


def test_coerce_for_pgtype_numbers_and_time():
    assert _coerce_for_pgtype("42", "int4") == 42
    assert _coerce_for_pgtype("3.5", "numeric") == 3.5
    assert _coerce_for_pgtype("2026-06-01 00:00:00", "timestamp") == datetime.datetime(2026, 6, 1)
    assert _coerce_for_pgtype("2026-06-01", "date") == datetime.date(2026, 6, 1)


def test_coerce_for_pgtype_text_and_arrays_passthrough():
    assert _coerce_for_pgtype("EMERGENCY", "text") == "EMERGENCY"
    assert _coerce_for_pgtype("1", "text") == "1"          # text column stays text
    assert _coerce_for_pgtype(["EMEA", "APAC"], "_text") == ["EMEA", "APAC"]
    assert _coerce_for_pgtype(5, "int4") == 5              # already typed passes through


def test_coerce_timestamp_string_to_datetime():
    assert _coerce_temporal("2026-06-01 00:00:00") == datetime.datetime(2026, 6, 1, 0, 0, 0)
    assert _coerce_temporal("2026-06-30 23:59:59") == datetime.datetime(2026, 6, 30, 23, 59, 59)
    assert _coerce_temporal("2026-06-01T00:00:00") == datetime.datetime(2026, 6, 1, 0, 0, 0)


def test_coerce_leaves_text_and_bare_dates_alone():
    # genuine text filters and bare dates must NOT be turned into datetimes
    assert _coerce_temporal("EMERGENCY") == "EMERGENCY"
    assert _coerce_temporal("2026-06-01") == "2026-06-01"
    assert _coerce_temporal(["EMEA", "APAC"]) == ["EMEA", "APAC"]
    assert _coerce_temporal(5) == 5


def test_scalar_placeholders():
    sql, params = to_asyncpg("a = %s AND b = %s", ["x", 3])
    assert sql == "a = $1 AND b = $2"
    assert params == ["x", 3]


def test_between():
    sql, params = to_asyncpg("t BETWEEN %s AND %s", ["2026-01-01", "2026-06-29"])
    assert sql == "t BETWEEN $1 AND $2"
    assert params == ["2026-01-01", "2026-06-29"]


def test_in_tuple_expands():
    sql, params = to_asyncpg("p IN %s", [("P1", "P2", "P3")])
    assert sql == "p IN ($1, $2, $3)"
    assert params == ["P1", "P2", "P3"]


def test_not_in_tuple_expands():
    sql, params = to_asyncpg("s NOT IN %s AND x = %s", [("CLOSED",), "y"])
    assert sql == "s NOT IN ($1) AND x = $2"
    assert params == ["CLOSED", "y"]


def test_list_stays_single_array_param():
    # = ANY(%s) / && %s carry the whole list as ONE param
    sql, params = to_asyncpg("r = ANY(%s)", [["EMEA", "APAC"]])
    assert sql == "r = ANY($1)"
    assert params == [["EMEA", "APAC"]]
    sql2, params2 = to_asyncpg("r && %s", [["a", "b"]])
    assert sql2 == "r && $1"
    assert params2 == [["a", "b"]]


def test_escaped_percent_unescaped():
    sql, params = to_asyncpg("x LIKE 'a%%b' AND y = %s", ["z"])
    assert sql == "x LIKE 'a%b' AND y = $1"
    assert params == ["z"]


def test_mixed_real_dsl_shape():
    # mirrors an emergency table query: static '=' + between + array_val filter
    sql, params = to_asyncpg(
        "WHERE a.type_description = %s AND t BETWEEN %s AND %s AND s && %s",
        ["EMERGENCY", "2026-01-01 00:00:00", "2026-06-29 23:59:59", ["Retail"]])
    assert sql == "WHERE a.type_description = $1 AND t BETWEEN $2 AND $3 AND s && $4"
    assert params == ["EMERGENCY", "2026-01-01 00:00:00", "2026-06-29 23:59:59", ["Retail"]]


def test_empty_tuple_guard():
    sql, params = to_asyncpg("p IN %s", [tuple()])
    assert sql == "p IN (NULL)"
    assert params == []


def test_resolve_dsn_prefers_named(monkeypatch):
    monkeypatch.setenv("CORA_DB_VTX5", "postgresql://named")
    monkeypatch.setenv("CORA_PG_DSN", "postgresql://generic")
    assert resolve_dsn("vtx5") == "postgresql://named"
    assert resolve_dsn("other") == "postgresql://generic"
    monkeypatch.delenv("CORA_DB_VTX5")
    monkeypatch.delenv("CORA_PG_DSN")
    assert resolve_dsn("vtx5") is None
