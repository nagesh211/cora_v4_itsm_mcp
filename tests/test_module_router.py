"""Tests for question -> module routing (module_router.detect_module).

Routing must pick the right module for clearly single-domain questions (so the KPI
search can be filtered to it), fold full/short forms and metric synonyms, avoid false
matches inside longer words ("cm" not inside "outcome"), and return None whenever it
isn't confident (nothing matches, or two modules tie) so the caller searches
unfiltered. Reads cora_mcp/module_catalog.json only -- no OpenSearch needed.
"""
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cora_mcp.module_router import detect_module, reset_cache  # noqa: E402


def setup_function(_):
    reset_cache()  # each test reads a fresh phrase index


def test_change_question_routes_to_cm():
    # the original failing example
    assert detect_module("percentage of changes by type") == "cm"


def test_incident_question_routes_to_im():
    assert detect_module("how many incidents last month") == "im"


def test_full_form_beats_stray_token():
    # "change management" (2 words) must dominate any incidental single-word hit
    assert detect_module("what is our change management volume") == "cm"


def test_short_form_standalone_matches():
    assert detect_module("show me sd call abandonment rate") == "sd"


def test_metric_synonym_routes():
    # routes via a metric synonym, not just a module alias
    assert detect_module("what is the call abandonment rate") == "sd"


def test_short_form_not_matched_inside_word():
    # "cm" must not fire on "outcome"; nothing else matches -> None
    assert detect_module("what was the outcome of the review") is None


def test_ambiguous_cross_module_returns_none():
    # equal-strength terms from two modules -> tie -> unfiltered
    assert detect_module("release or problem this week") is None


def test_no_match_returns_none():
    assert detect_module("hello there") is None
    assert detect_module("") is None