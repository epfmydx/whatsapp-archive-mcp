"""Opt-in PostgreSQL ordering, duplicate and FK-lock regression checks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import select

from app.models import Event, Message, MessageState, MessageUpdate
from test_auth import BASE
from test_api import payload
from test_message_updates import edit_payload, update_payload
from test_registration_postgres import postgres_registration

HEADERS = {"Authorization": "Bearer "+"i"*32}


def concurrently(application, requests):
    barrier = Barrier(len(requests))
    def send(item):
        path, data = item
        with application.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(path, base_url=BASE, headers=HEADERS, json=data)
            assert response.status_code == 200, response.json
            assert response.headers["X-Archive-Format"] == "2"
            return response.json
    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        return list(executor.map(send, requests))


def test_original_duplicates_and_updates_serialize_without_fk_deadlock(postgres_registration):
    application, _ = postgres_registration
    result = concurrently(application, [
        ("/internal/messages", payload()),
        ("/internal/messages", payload()),
        ("/internal/message-updates", update_payload(1)),
        ("/internal/message-updates", update_payload(1)),
        ("/internal/message-updates", update_payload(2, kind="delete")),
        ("/internal/message-updates", update_payload(3, kind="reaction", reaction={"emoji": "👍"})),
    ])
    assert sorted(row["duplicate"] for row in result[:2]) == [False, True]
    assert sorted(row["duplicate"] for row in result[2:4]) == [False, True]
    with application.extensions["db_session"]() as db:
        messages = list(db.scalars(select(Message)))
        assert len(messages) == 1 and messages[0].text == "test hello"
        state = db.get(MessageState, ("personal", "chat@example", "wa-test"))
        assert state.effective_text == "edited text" and state.deleted_at is not None
        assert len(list(db.scalars(select(MessageUpdate)))) == 3
        assert len(list(db.scalars(select(Event)))) == 4


def test_concurrent_edits_choose_latest_order_independent_of_lock_winner(postgres_registration):
    application, _ = postgres_registration
    with application.test_client() as client:
        assert client.post("/internal/messages", base_url=BASE, headers=HEADERS, json=payload()).status_code == 200
    bodies = [update_payload(number, edit=edit_payload(text="value "+str(number))) for number in range(4)]
    winner = max(bodies, key=lambda row: (row["timestamp"], row["event_id"]))
    concurrently(application, [("/internal/message-updates", body) for body in bodies])
    with application.extensions["db_session"]() as db:
        state = db.get(MessageState, ("personal", "chat@example", "wa-test"))
        assert state.effective_text == winner["edit"]["text"]
        assert state.edit_event_id == winner["event_id"]
