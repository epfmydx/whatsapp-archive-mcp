"""Unavailable-content contracts, using synthetic messages only."""
import json

import pytest

from app.models import MessageMetadata
from test_api import payload
from test_auth import BASE
from test_message_updates import access, call, edit_payload, ingest, update

RETAINED = {"reason": "unsupported_message_type", "message_fields": ["pollUpdateMessage"],
            "recovery": "operator_retained_payload"}


def test_retained_unknown_round_trip_search_and_original_preservation(client, internal, access, application):
    ident = ingest(client, internal, message_type="unknown", text="", content=RETAINED, content_truncated=True)
    item = call(client, access, "get_message", id=ident)
    assert item["content"] == RETAINED and item["media"] is None and item["transcription"] is None
    assert call(client, access, "search_messages", query="")["items"][0]["content"] == RETAINED
    with application.extensions["db_session"]() as db:
        row = db.get(MessageMetadata, ("personal", "chat@example", "wa-test"))
        assert json.loads(row.envelope)["content"] == RETAINED


def test_old_unknown_is_explained_without_claiming_recovery(client, internal, access, application):
    ident = ingest(client, internal, message_type="unknown", text="", content={}, content_truncated=True)
    assert call(client, access, "get_message", id=ident)["content"] == {
        "reason": "unrecognized_legacy_content", "recovery": "unknown"}
    with application.extensions["db_session"]() as db:
        assert json.loads(db.get(MessageMetadata, ("personal", "chat@example", "wa-test")).envelope)["content"] == {}


def test_unknown_edit_is_explicit_and_deletion_still_masks_it(client, internal, access):
    ident = ingest(client, internal)
    update(client, internal, edit=edit_payload(message_type="unknown", text="", content=RETAINED, content_truncated=True))
    assert call(client, access, "get_message", id=ident)["content"] == RETAINED
    update(client, internal, 2, kind="delete")
    item = call(client, access, "get_message", id=ident)
    assert item["is_deleted"] and item["content"] == {} and item["text"] == ""


@pytest.mark.parametrize("content", [
    {"reason": "raw private failure"}, {"reason": []}, {"recovery": "https://private.test"},
    {"message_fields": "pollUpdateMessage"}, {"message_fields": ["a"] * 33},
    {"message_fields": ["pollUpdateMessage", "pollUpdateMessage"]},
    {"message_fields": ["https://private.test"]}, {"message_fields": ["a" * 81]},
    {"message_fields": [{}]}, {"raw_proto": "private"},
])
def test_unknown_diagnostics_reject_arbitrary_or_unbounded_values(client, internal, content):
    response = client.post("/internal/messages", base_url=BASE, headers=internal,
                           json=payload(message_type="unknown", content=content))
    assert response.status_code == 400


def test_unsupported_metadata_is_not_accepted_on_recognized_content(client, internal):
    response = client.post("/internal/messages", base_url=BASE, headers=internal,
                           json=payload(message_type="text", content=RETAINED))
    assert response.status_code == 400
