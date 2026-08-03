"""Tests for ``cora_mcp.column_resolver`` — table-scoped word -> column resolution.

The behaviour worth pinning is that resolution honours a column's **declared
vocabulary** (``canonical`` / ``alias`` in ``schema_v3.yaml``). That metadata was
authored long ago and passed through to ``describe_dataset``, but no resolution path
consulted it, so a question phrased in the vocabulary the schema itself documents was
rejected. Also pinned: resolution is *per table* (the same word means different columns
on different tables) and never a substring match.
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp.column_resolver import (resolve_column,  # noqa: E402
                                      resolve_column_detail, resolvable_words)

INCIDENTS = "itsm_incident.tbl_all_incidents"
MAJOR_SLA = "itsm_availability.tbl_tableau_major_incdnt"
INCIDENT_SLA = "itsm_incident.tbl_incident_sla"
SLA_RESOLUTION = "itsm_incident.tbl_sla_resolution"


@pytest.mark.parametrize("word,expected", [
    ("business_name", "business_name"),        # exact column name
    ("business", "business_name"),             # suffix form
    ("sector", "business_name"),               # declared canonical/alias
    ("region", "region_name"),
    ("capability", "service_area"),            # alias -> a differently-named column
    ("category", "category_description"),
    ("priority", "priority_code"),
    ("status", "status_name"),
    ("vendor", "it_vendor_name"),              # canonical: vendor
])
def test_resolves_declared_vocabulary_on_incidents(word, expected):
    assert resolve_column(INCIDENTS, word) == expected


def test_major_incident_word_resolves_via_declared_canonical():
    """The column is major_incident_indicator; the schema declares
    ``canonical: major incident``, so the phrase must reach it."""
    assert resolve_column(INCIDENTS, "major incident") == "major_incident_indicator"


def test_resolution_is_case_and_separator_insensitive():
    for variant in ("Business Name", "business-name", "BUSINESS_NAME", " business  name "):
        assert resolve_column(INCIDENTS, variant) == "business_name"


def test_unknown_word_returns_none_rather_than_a_neighbour():
    """Substituting a plausible neighbour would silently answer a different question."""
    assert resolve_column(INCIDENTS, "unicorn_colour") is None
    assert resolve_column(INCIDENTS, "") is None


def test_no_substring_collisions():
    """'sub business' must not be answered by the 'business' column."""
    assert resolve_column(INCIDENTS, "sub business") == "sub_business_name"
    assert resolve_column(INCIDENTS, "division") == "sub_business_name"


def test_resolution_is_per_table_not_global():
    """The same concept lives under different column names per table — resolution must
    be scoped, which is why the composer resolves against each candidate anchor."""
    assert resolve_column(INCIDENTS, "priority") == "priority_code"
    assert resolve_column(MAJOR_SLA, "priority") == "priority_description"
    # and a column simply absent on that table must not resolve at all
    assert resolve_column(MAJOR_SLA, "business") is None


def test_roles_restrict_group_by_targets():
    """A breakdown must never group by a timestamp or an identifier."""
    assert resolve_column(INCIDENTS, "open_date_time", roles=("dimension",)) is None
    assert resolve_column(INCIDENTS, "open_date_time", roles=()) == "open_date_time"
    assert resolve_column(INCIDENTS, "incident_id", roles=("dimension",)) is None


def test_unknown_table_resolves_to_none():
    assert resolve_column("nope.nope", "business") is None


# ---------------------------------------------------------------------------
# near misses — a name that differs only by inflection or timestamp precision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table,word,expected", [
    # the reported failure: `close_date_time` named a clock the table calls `closed_date`
    (INCIDENT_SLA, "close_date_time", "closed_date"),
    (INCIDENT_SLA, "create_date", "created_date"),
    (INCIDENTS, "opened_date", "open_date"),
    (INCIDENTS, "resolve_date_time", "resolved_date_time"),
])
def test_inflection_and_precision_variants_resolve(table, word, expected):
    """Refusing a typo-grade difference costs the user a round trip they can only
    answer by reading the schema back to us, so it is resolved instead — and the
    caller is told, via the `near_miss` marker, which column actually ran."""
    assert resolve_column(table, word) == expected
    assert resolve_column_detail(table, word)[1] == "near_miss"


def test_the_nearest_clock_wins_when_several_are_close():
    """A table carrying both precisions must map each request to its own: answering
    `close_date_time` with the date-only column when the timestamp exists would
    quietly change the boundaries of every window."""
    assert resolve_column(SLA_RESOLUTION, "close_date_time") == "closed_date_time"
    assert resolve_column(SLA_RESOLUTION, "close_date") == "closed_date"


def test_a_near_miss_never_crosses_to_a_different_subject():
    """`closed` and `created` are different clocks; a shared shape is not a match."""
    assert resolve_column(INCIDENT_SLA, "closure_timestamp") is None
    assert resolve_column(INCIDENTS, "unicorn_date") is None


def test_a_near_miss_needs_both_a_subject_and_a_matching_shape():
    """'date time' names no subject, and a bare 'closed' is not evidence enough to
    pick a timestamp — both must stay unresolved rather than land on a plausible
    neighbour."""
    assert resolve_column(INCIDENT_SLA, "date time") is None
    assert resolve_column(INCIDENT_SLA, "closed") is None


def test_exact_and_declared_names_are_never_reported_as_approximate():
    """Only a genuine near miss is worth warning the user about — a name the schema
    itself declares is an exact answer and must not be caveated as a guess."""
    assert resolve_column_detail(INCIDENTS, "business_name")[1] == "exact"
    assert resolve_column_detail(INCIDENTS, "sector")[1] == "vocabulary"
    assert resolve_column_detail(INCIDENTS, "business")[1] in ("alias", "suffix")


def test_resolvable_words_includes_names_and_vocabulary():
    words = resolvable_words(INCIDENTS)
    assert "business_name" in words          # real column name
    assert "sector" in words                 # declared alias
    assert len(words) == len(set(words))     # deduplicated, for clean error messages