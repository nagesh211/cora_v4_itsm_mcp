"""Fan-out guard: a 1:N join must not silently inflate a measure.

The scenario throughout is ``incidents -> changes``, which the schema declares as
``incident_caused_by_change``: a ``via`` edge through ``tbl_incident_change_relation``.
A junction table is many-to-many by construction, so one incident can produce
several joined rows and every naive aggregate over that result is too large.
"""
import pytest

from cora_mcp import fanout
from cora_mcp import sql_builder as sb
from cora_mcp.sql_builder import BuilderError

INC = "itsm_incident.tbl_all_incidents"
CHG = "itsm_change.tbl_change"


def _spec(**kw):
    spec = {"base": INC, "join_with": [CHG]}
    spec.update(kw)
    return spec


# ---------------------------------------------------------------------------
# edge classification
# ---------------------------------------------------------------------------
def test_via_edge_always_fans_out():
    """A junction table is many-to-many; no declaration can make it safe."""
    rel = {"name": "r", "left": "A", "right": "B", "via": "J",
           "cardinality": "one_to_one"}
    assert fanout.edge_fans_out(rel, "A") is True


def test_undeclared_cardinality_is_assumed_unsafe():
    rel = {"name": "r", "left": "A", "right": "B", "join_on": {}}
    assert fanout.edge_fans_out(rel, "A") is True


def test_declared_many_to_one_is_safe_only_from_the_many_side():
    rel = {"name": "r", "left": "A", "right": "B", "join_on": {},
           "cardinality": "many_to_one"}
    assert fanout.edge_fans_out(rel, "A") is False   # many A -> one B: no multiplication
    assert fanout.edge_fans_out(rel, "B") is True    # one B -> many A: multiplies


def test_classify_cardinality_from_measured_uniqueness():
    assert fanout.classify_cardinality(True, True) == "one_to_one"
    assert fanout.classify_cardinality(False, True) == "many_to_one"
    assert fanout.classify_cardinality(True, False) == "one_to_many"
    assert fanout.classify_cardinality(False, False) == "many_to_many"


# ---------------------------------------------------------------------------
# count(*) is corrected
# ---------------------------------------------------------------------------
def test_count_star_is_deduplicated_on_base_row_identity():
    built = sb.build(_spec())
    assert "count(DISTINCT a.ctid)" in built.sql
    assert "count(*)" not in built.sql
    assert built.fanning_relationships == ["incident_caused_by_change"]
    assert "distinct base rows" in built.fanout_note


def test_count_star_is_untouched_without_a_join():
    built = sb.build({"base": INC})
    assert "count(*)" in built.sql
    assert "ctid" not in built.sql
    assert built.fanning_relationships == []
    assert built.fanout_note is None


# ---------------------------------------------------------------------------
# aggregates that cannot be corrected are refused, not guessed
# ---------------------------------------------------------------------------
def test_sum_on_the_base_table_across_a_fanning_join_is_refused():
    with pytest.raises(BuilderError) as exc:
        sb.build(_spec(measure={"agg": "sum", "column": "reopen_count"}))
    msg = str(exc.value)
    assert "cannot compute sum correctly" in msg
    assert "incident_caused_by_change" in msg
    assert "semi-join" in msg          # the message names the way out


def test_avg_is_refused_for_the_same_reason():
    with pytest.raises(BuilderError):
        sb.build(_spec(measure={"agg": "avg", "column": "reopen_count"}))


def test_sum_is_fine_when_nothing_fans_out():
    built = sb.build({"base": INC, "measure": {"agg": "sum", "column": "reopen_count"}})
    assert "sum(a.reopen_count)" in built.sql


# ---------------------------------------------------------------------------
# aggregates that duplication cannot change pass through
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("agg,frag", [
    ("count_distinct", "count(distinct"),
    ("min", "min("),
    ("max", "max("),
])
def test_invariant_aggregates_are_left_alone(agg, frag):
    built = sb.build(_spec(measure={"agg": agg, "column": "incident_id"}))
    assert frag in built.sql
    assert "ctid" not in built.sql


# ---------------------------------------------------------------------------
# the measured column's home table decides whether it is at risk
# ---------------------------------------------------------------------------
def test_measure_on_the_joined_table_keeps_its_own_grain():
    """Summing a column that lives on the many-side is not inflation -- those rows
    ARE the grain the question means."""
    sql, note = fanout.correct_measure("sum", "b.cost", "value", "a", ["rel"],
                                       home_is_base=False)
    assert sql == "sum(b.cost) AS value"
    assert "grain of the table joined via rel" in note


def test_two_fanning_edges_make_even_the_joined_side_untrustworthy():
    with pytest.raises(fanout.FanoutError):
        fanout.correct_measure("sum", "b.cost", "value", "a", ["rel1", "rel2"],
                               home_is_base=False)


# ---------------------------------------------------------------------------
# guard modes
# ---------------------------------------------------------------------------
def test_warn_mode_allows_the_sum_but_labels_it(monkeypatch):
    monkeypatch.setenv("CORA_FANOUT_GUARD", "warn")
    sql, note = fanout.correct_measure("sum", "a.x", "value", "a", ["rel"])
    assert sql == "sum(a.x) AS value"
    assert note.startswith("WARNING")


def test_off_mode_restores_legacy_behaviour(monkeypatch):
    monkeypatch.setenv("CORA_FANOUT_GUARD", "off")
    sql, note = fanout.correct_measure("count", None, "value", "a", ["rel"])
    assert sql == "count(*) AS value"
    assert note is None


def test_unrecognised_mode_falls_back_to_strict(monkeypatch):
    monkeypatch.setenv("CORA_FANOUT_GUARD", "banana")
    assert fanout.guard_mode() == "strict"