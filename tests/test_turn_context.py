"""Tests for the web UI's per-turn context carry-over.

A follow-up ("show me details for those incidents") used to be rewritten from the
prose summary alone, so the filters/period/ids of the previous turn could silently
vanish and the analyst then queried something that matched nothing — the "No data
available" report. ``_turn_context`` captures the facts of a turn; these tests pin
what it keeps.
"""
import json

RUN_KPI_RESULT = {
    "kpi": "im-major-incidents",
    "title": "Major Incidents Count",
    "mode": "table",
    "dimension": "priority_code",
    "filters": {"sector": ["FINANCE"]},
    "resolved_from_phrase": {"phrase": "last month", "matched": True,
                             "start_date": "2026-06-01", "end_date": "2026-06-30"},
    "results": [{"label": "table window", "rows": [{"grp": "P1", "v": 12},
                                                   {"grp": "P2", "v": 30}]}],
}

LISTING_RESULT = {
    "base_table": "itsm_incident.tbl_all_incidents",
    "sql": "SELECT ...",
    "date_window": {"start_date": "2026-06-01", "end_date": "2026-06-30"},
    "rows": [{"incident_id": "INC0364440", "priority_code": "P1"},
             {"incident_id": "INC0364512", "priority_code": "P1"}],
}


def _outputs(*pairs):
    return [{"name": name, "is_error": False, "result": res} for name, res in pairs]


def test_keeps_metric_filters_period_and_group_labels():
    from clients.web_ui import _turn_context
    ctx = _turn_context("major incidents last month by priority for finance",
                        _outputs(("run_kpi", RUN_KPI_RESULT)), "12 P1 and 30 P2.")
    entry = ctx["data"][0]
    assert entry["metric"] == "im-major-incidents"
    assert entry["filters"] == {"sector": ["FINANCE"]}
    assert entry["period"] == "last month"
    assert entry["dimension"] == "priority_code"
    assert entry["group_labels"] == ["P1", "P2"]
    assert entry["rowcount"] == 2
    assert ctx["answer"] == "12 P1 and 30 P2."


def test_keeps_record_ids_from_a_detail_listing():
    from clients.web_ui import _turn_context
    ctx = _turn_context("list the p1 incidents",
                        _outputs(("query_dataset", LISTING_RESULT)), None)
    entry = ctx["data"][0]
    assert entry["record_ids"] == ["INC0364440", "INC0364512"]
    assert entry["entity"] == "itsm_incident.tbl_all_incidents"
    assert entry["window"]["start_date"] == "2026-06-01"


def test_ignores_discovery_tools_but_still_records_the_answer():
    from clients.web_ui import _turn_context
    ctx = _turn_context("what modules are there?",
                        _outputs(("search_kpis", [{"name": "x"}])), "Eight modules.")
    assert "data" not in ctx
    assert ctx["answer"] == "Eight modules."


def test_no_context_when_nothing_happened():
    from clients.web_ui import _turn_context
    assert _turn_context("hello", [], None) is None


def test_comparison_windows_are_carried_forward():
    from clients.web_ui import _turn_context
    result = {"kpi": "availability-percentage",
              "resolved_from_phrase": {"phrase": "last quarter vs current quarter"},
              "comparison_windows": {"previous": {"phrase": "last quarter"},
                                     "current": {"phrase": "current quarter"}},
              "results": [{"rows": [{"v": 98.5}]}, {"rows": [{"v": 99.2}]}]}
    ctx = _turn_context("compare availability", _outputs(("run_kpi", result)), "ok")
    assert ctx["data"][0]["window"]["current"]["phrase"] == "current quarter"


def test_row_ids_only_accepts_identifier_columns():
    from clients.web_ui import _row_ids
    rows = [{"description_text": "ABC1234567 mentioned here", "incident_id": "INC001234"},
            {"description_text": "no id column"},
            {"change_number": "CHG0009999"}]
    assert _row_ids(rows) == ["INC001234", "CHG0009999"]


def test_row_ids_are_capped():
    from clients.web_ui import _MAX_IDS, _row_ids
    rows = [{"incident_id": "INC%07d" % i} for i in range(200)]
    assert len(_row_ids(rows)) == _MAX_IDS


def test_last_result_block_is_attached_and_bounded():
    from clients.web_ui import _with_last_result
    assert _with_last_result("SYSTEM", None) == "SYSTEM"
    out = _with_last_result("SYSTEM", {"question": "q", "data": [{"tool": "run_kpi"}]})
    assert out.startswith("SYSTEM")
    assert "<last_result>" in out and "</last_result>" in out
    body = out.split("<last_result>")[1].split("</last_result>")[0].strip()
    assert json.loads(body)["data"][0]["tool"] == "run_kpi"
    big = _with_last_result("SYSTEM", {"answer": "x" * 20000})
    assert len(big) < 6000