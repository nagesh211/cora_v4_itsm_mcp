"""Tests for ``cora_mcp.sql_alias`` — qualifying injected filter columns.

A SQL-mode KPI inlines user filters at its authored ``{filters}`` slot using each
field's ``fields[*].column``. When that column is bare and the authored query
joins two tables that both carry it, Postgres rejects the whole query with
"column reference ... is ambiguous". These tests pin the alias resolution and,
importantly, the cases where it must NOT touch anything.
"""
import copy
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gen_query as gq  # noqa: E402
from cora_mcp import sql_alias  # noqa: E402

# Two real tables that BOTH declare business_name / region_name in schema_v3.yaml —
# the exact shape that produced the ambiguity in production.
BASE = "itsm_incident.tbl_major_incidents"
JOINED = "itsm_availability.tbl_tableau_major_incdnt"

JOINED_QUERY = (
    "select count(distinct a.incident_id) as v "
    f"from {BASE} a "
    f"inner join {JOINED} b on a.incident_system_id = b.incident_system_id "
    "where (a.closed_date BETWEEN '{from_date}' AND '{to_date}' {filters})"
)


def _config(base_query=JOINED_QUERY, **overrides):
    cfg = {
        "name": "test-kpi",
        "execution_mode": "SQL",
        "source": {"connection": "vtx5", "dialect": "postgres",
                   "schema": "itsm_incident"},
        "primary_dataset": {"name": "tbl_major_incidents",
                            "schema": "itsm_incident",
                            "table": "tbl_major_incidents"},
        "fields": {
            "sector": {"dataset": "tbl_major_incidents", "column": "business_name",
                       "filter_type": "array_val"},
            "region": {"dataset": "tbl_major_incidents", "column": "region_name",
                       "filter_type": "in"},
            "already": {"dataset": "tbl_major_incidents", "column": "b.business_name",
                        "filter_type": "in"},
            "nowhere": {"dataset": "tbl_major_incidents", "column": "not_a_column",
                        "filter_type": "in"},
        },
        "filters": {"allowed": ["sector", "region", "already", "nowhere"]},
        "sql": {"base_query": base_query},
    }
    cfg.update(overrides)
    return cfg


def _column(cfg, key):
    return cfg["fields"][key]["column"]


# ---------------------------------------------------------------------------
# alias scope
# ---------------------------------------------------------------------------
def test_alias_bindings_reads_from_and_join_clauses():
    assert sql_alias.alias_bindings(JOINED_QUERY) == [(BASE, "a"), (JOINED, "b")]


def test_alias_bindings_ignores_keywords_following_a_table():
    # `FROM schema.table WHERE ...` has no alias — "where" must not become one.
    assert sql_alias.alias_bindings(f"select 1 from {BASE} where x = 1") == []


def test_slot_scope_picks_the_primary_tables_alias():
    alias, scope = sql_alias.slot_scope(JOINED_QUERY, BASE)
    assert alias == "a"
    assert scope == {BASE: "a", JOINED: "b"}


def test_slot_scope_none_without_a_filters_slot_or_a_join():
    assert sql_alias.slot_scope("select 1 from x.y a", BASE) is None          # no slot
    single = f"select 1 from {BASE} a where (1=1 {{filters}})"
    assert sql_alias.slot_scope(single, BASE) is None      # one table: never ambiguous


def test_slot_scope_none_when_two_slots_need_different_aliases():
    # One `fields` mapping cannot serve two different scopes, so nothing is touched
    # rather than half of it mis-qualified.
    two = (f"select * from (select 1 from {BASE} a where (1=1 {{filters}})) x "
           f"full join (select 2 from {JOINED} c where (1=1 {{filters}})) y on true")
    assert sql_alias.slot_scope(two, BASE) is None


# ---------------------------------------------------------------------------
# qualification
# ---------------------------------------------------------------------------
def test_shared_column_is_qualified_with_the_primary_alias():
    out = sql_alias.qualify_filter_columns(_config(), ["sector", "region"])
    assert _column(out, "sector") == "a.business_name"
    assert _column(out, "region") == "a.region_name"


def test_qualification_does_not_mutate_the_input_config():
    cfg = _config()
    snapshot = copy.deepcopy(cfg)
    sql_alias.qualify_filter_columns(cfg, ["sector"])
    assert cfg == snapshot


def test_declared_type_is_carried_over():
    # gen_query gives up on resolving the type of a qualified column, and without a
    # type a text column would fall back to lower(col::text) — so the type moves
    # onto the field meta and the emitted predicate stays identical.
    out = sql_alias.qualify_filter_columns(_config(), ["region"])
    assert out["fields"]["region"]["type"]
    assert gq._column_type(out, "region", out["fields"]) == \
           gq._column_type(_config(), "region", _config()["fields"])


def test_already_qualified_and_unknown_columns_are_left_alone():
    out = sql_alias.qualify_filter_columns(_config(), ["already", "nowhere"])
    assert _column(out, "already") == "b.business_name"   # author's own alias kept
    # the schema proves nothing about `not_a_column`, so we do not guess an alias:
    # a genuinely unique column still resolves unqualified.
    assert _column(out, "nowhere") == "not_a_column"


def test_dsl_configs_are_untouched():
    cfg = _config(execution_mode="DSL")
    assert sql_alias.qualify_filter_columns(cfg, ["sector"]) is cfg


def test_single_table_config_emits_identical_sql():
    single = f"select count(*) as v from {BASE} a where (1=1 {{filters}})"
    cfg = _config(base_query=single)
    assert sql_alias.qualify_filter_columns(cfg, ["region"]) is cfg


# ---------------------------------------------------------------------------
# end-to-end: the generated predicate
# ---------------------------------------------------------------------------
def _sql_for(cfg, filter_by):
    payload = gq.build_payload(cfg, "stat",
                               ("2026-04-01 00:00:00", "2026-06-30 23:59:59"),
                               filter_by, None, None)
    return gq.build_sql(cfg, payload)[0]


def test_generated_predicate_is_qualified_end_to_end():
    filter_by = {"sector": ["FINANCE"]}
    before = _sql_for(_config(), filter_by)
    after = _sql_for(sql_alias.qualify_filter_columns(_config(), list(filter_by)),
                     filter_by)
    assert "unnest(business_name)" in before          # the ambiguous form
    assert "unnest(a.business_name)" in after
    assert "unnest(business_name)" not in after


def test_every_shipped_sql_config_only_qualifies_proven_columns():
    """No shipped config gets an alias the schema can't justify.

    A guessed alias would turn a working query into "column a.x does not exist",
    so this walks every SQL-mode config with a {filters} slot and asserts each
    rewritten column really belongs to the table that alias is bound to.
    """
    import glob
    import json
    types = sql_alias._schema_types()
    checked = 0
    for path in sorted(glob.glob(os.path.join(_ROOT, "config", "*.json"))) + \
            sorted(glob.glob(os.path.join(_ROOT, "pepops_bkp", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if cfg.get("execution_mode") == "DSL":
            continue
        scope = sql_alias.slot_scope(((cfg.get("sql") or {}).get("base_query")) or "",
                                     sql_alias._primary_fqn(cfg))
        if not scope:
            continue
        alias_to_table = {a.lower(): t for t, a in scope[1].items()}
        for key in (cfg.get("filters") or {}).get("allowed") or []:
            old = ((cfg.get("fields") or {}).get(key) or {}).get("column")
            if not old or "." in old:
                continue
            new = _column(sql_alias.qualify_filter_columns(cfg, [key]), key)
            if new == old:
                continue                     # left bare on purpose
            alias, column = new.split(".", 1)
            table = alias_to_table[alias.lower()]
            assert column in (types.get(table) or {}), \
                f"{os.path.basename(path)}:{key} -> {new} (not a column of {table})"
            checked += 1
    assert checked > 0, "no shipped SQL config exercised the qualifier"