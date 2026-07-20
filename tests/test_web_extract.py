"""Tests for the web UI's tool-result unwrapping."""
import json


def test_coerce_mcp_content_list():
    from clients.web_ui import _coerce
    payload = {"kpi": "emergency", "results": [{"label": "x"}]}
    # autogen delivers MCP tool output as a list of text content parts
    wrapped = [{"type": "text", "text": json.dumps(payload)}]
    assert _coerce(wrapped) == payload


def test_coerce_json_string_of_content_list():
    # the shape autogen actually delivers: a JSON *string* of a content-part list
    from clients.web_ui import _coerce
    payload = {"kpi": "emergency", "results": [{"label": "x"}]}
    wrapped = json.dumps([{"type": "text", "text": json.dumps(payload)}])
    assert _coerce(wrapped) == payload


def test_coerce_plain_json_string():
    from clients.web_ui import _coerce
    assert _coerce('{"a": 1}') == {"a": 1}


def test_coerce_non_json_string_passthrough():
    from clients.web_ui import _coerce
    assert _coerce("hello") == "hello"


def test_coerce_already_dict():
    from clients.web_ui import _coerce
    assert _coerce({"a": 1}) == {"a": 1}
