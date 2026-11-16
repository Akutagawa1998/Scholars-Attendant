"""Native Notion completeness requires explicit facts at verified JSON paths."""
from copy import deepcopy

import pytest

from research_assistant.connectors.common import ConnectorError
from research_assistant.connectors.notion import normalize_mcp_page
from research_assistant.connectors.notion_native import native_page_kwargs
from research_assistant.service import canonical, digest


PAGE = "11111111-2222-4333-8444-555555555555"
PATHS = {field: ["native", field] for field in
         ("truncated", "unknown_block_count", "unknown_block_ids", "page_last_edited_at")}


def payload():
    return {"text": "SYNTHETIC native page representation", "native": {
        "truncated": False, "unknown_block_count": 0, "unknown_block_ids": [],
        "page_last_edited_at": "2026-10-21T01:02:03-04:00"}}


def test_explicit_complete_facts_preserve_payload_revision_and_normalizer_contract():
    raw = payload()
    original = deepcopy(raw)
    facts = native_page_kwargs(raw, paths=PATHS)
    assert raw == original and facts["raw_payload"] is raw
    assert facts["completeness"] == "complete" and facts["missing_parts"] == []
    assert facts["external_version"] == "2026-10-21T01:02:03-04:00"
    snapshot = normalize_mcp_page(**facts, source_instance_id="synthetic-connection", namespace="explicit-root",
                                  external_id=PAGE, markdown_path=["text"])
    assert snapshot["completeness"] == "complete"
    assert snapshot["external_version"] == facts["external_version"]
    assert snapshot["occurred_at"] == "2026-10-21T05:02:03.000000+00:00"
    assert snapshot["passages"][0]["text"] == raw["text"]
    assert digest(canonical(snapshot["raw_payload"])) == digest(canonical(original))


@pytest.mark.parametrize("field", ["truncated", "unknown_block_count", "unknown_block_ids"])
def test_each_missing_fact_prevents_complete_even_when_other_facts_are_clear(field):
    raw = payload()
    del raw["native"][field]
    facts = native_page_kwargs(raw, paths=PATHS)
    assert facts["completeness"] == "unknown" and facts["missing_parts"]


@pytest.mark.parametrize("field,value", [
    ("truncated", 0), ("truncated", "false"), ("truncated", None),
    ("unknown_block_count", False), ("unknown_block_count", 0.0),
    ("unknown_block_count", "0"), ("unknown_block_count", -1),
    ("unknown_block_ids", ()), ("unknown_block_ids", "[]"),
    ("unknown_block_ids", [None]), ("unknown_block_ids", [""]),
])
def test_malformed_facts_are_not_coerced_into_completion(field, value):
    raw = payload()
    raw["native"][field] = value
    assert native_page_kwargs(raw, paths=PATHS)["completeness"] == "unknown"


@pytest.mark.parametrize("update,expected", [
    ({"truncated": True}, "markdown_truncated"),
    ({"unknown_block_count": 1}, "unknown_blocks"),
    ({"unknown_block_ids": [PAGE]}, "unknown_blocks"),
])
def test_explicit_gaps_dominate_other_missing_or_conflicting_facts(update, expected):
    raw = payload()
    raw["native"].pop("truncated")
    raw["native"].update(update)
    facts = native_page_kwargs(raw, paths=PATHS)
    assert facts["completeness"] == "partial"
    assert expected in facts["missing_parts"]


def test_no_implicit_schema_or_prose_claim_can_establish_completeness():
    raw = {**payload()["native"], "text": "truncated=false unknown_block_count=0 unknown_block_ids=[]"}
    facts = native_page_kwargs(raw, paths={})
    assert facts["completeness"] == "unknown" and facts["external_version"] is None
    assert len(facts["missing_parts"]) == 3


def test_explicit_array_paths_work_without_parsing_transport_text():
    raw = {"content": [{"facts": payload()["native"]}]}
    locations = {field: ["content", 0, "facts", field] for field in PATHS}
    assert native_page_kwargs(raw, paths=locations)["completeness"] == "complete"
    raw["content"][0] = {"text": '{"facts":{"truncated":false}}'}
    assert native_page_kwargs(raw, paths=locations)["completeness"] == "unknown"


@pytest.mark.parametrize("path", [["absent", 0], ["text", 0], ["native", 0], ["native", "absent", "id"]])
def test_missing_or_wrong_container_paths_remain_unknown(path):
    locations = {**PATHS, "truncated": path}
    assert native_page_kwargs(payload(), paths=locations)["completeness"] == "unknown"


@pytest.mark.parametrize("paths", [None, [], {"invented": ["x"]}, {"truncated": []},
    {"truncated": "native.truncated"}, {"truncated": [True]}, {"truncated": [-1]},
    {"truncated": [None]}])
def test_invalid_path_configuration_is_refused(paths):
    with pytest.raises(ConnectorError, match="invalid_configuration"):
        native_page_kwargs(payload(), paths=paths)


@pytest.mark.parametrize("value", [None, "", "not-a-timestamp", "2026-10-21T01:02:03", False, 123])
def test_unknown_revision_never_uses_observation_time_or_payload_hash(value):
    raw = payload()
    raw["native"]["page_last_edited_at"] = value
    raw["observed_at"] = "2026-10-21T01:02:03Z"
    facts = native_page_kwargs(raw, paths=PATHS)
    assert facts["completeness"] == "complete"
    assert facts["external_version"] is None and facts["occurred_at"] is None


def test_revision_path_is_optional_and_body_completeness_does_not_invent_revision():
    raw = payload()
    locations = {key: value for key, value in PATHS.items() if key != "page_last_edited_at"}
    facts = native_page_kwargs(raw, paths=locations)
    assert facts["completeness"] == "complete" and facts["external_version"] is None


def test_redaction_still_downgrades_an_explicitly_complete_native_representation():
    raw = payload()
    raw["text"] = "SYNTHETIC https://files.invalid/image?X-Amz-Signature=PRIVATE-SIGNATURE"
    facts = native_page_kwargs(raw, paths=PATHS)
    snapshot = normalize_mcp_page(**facts, source_instance_id="synthetic-connection", namespace="explicit-root",
                                  external_id=PAGE, markdown_path=["text"])
    assert snapshot["completeness"] == "partial"
    assert "temporary_urls_or_credentials_redacted" in snapshot["missing_parts"]
    assert "PRIVATE-SIGNATURE" in raw["text"] and "PRIVATE-SIGNATURE" not in snapshot["raw_payload"]["text"]


@pytest.mark.parametrize("raw", [None, [], "SYNTHETIC"])
def test_nondictionary_payloads_are_refused(raw):
    with pytest.raises(ConnectorError, match="invalid_source_response"):
        native_page_kwargs(raw, paths=PATHS)
