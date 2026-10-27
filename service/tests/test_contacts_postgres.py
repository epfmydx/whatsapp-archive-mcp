"""Real PostgreSQL JSON scalar projection and contact-search regressions."""
from app.api import tool
from app.identities import identity_page, ingest_identities, set_alias
from app.message_updates import record_original
from test_identities import GROUP, LID, PN, link, message, session
from test_registration_postgres import postgres_registration


def test_postgres_reply_identity_projection_and_search(postgres_registration):
    application, _ = postgres_registration
    with session(application) as db:
        item = message(db, chat=GROUP)
        record_original(item, {"text": "private", "reply_to": {"message_id": "missing", "sender_jid": LID}})
        assert LID in identity_page("personal")["items"]
        ingest_identities(link())
        set_alias("personal", LID, "Known author 100%_")
        args = {"account_id": "personal", "query": "author 100%_", "limit": 1}
        first = tool("search_contacts", args, "owner")
        second = tool("search_contacts", {**args, "cursor": first["next_cursor"]}, "owner")
        assert [row["contact_id"] for row in first["items"]+second["items"]] == sorted([PN, LID])
        assert first["has_more"] and not second["has_more"]
        assert tool("search_contacts", {**args, "query": "author 100X_"}, "owner")["items"] == []
        result = tool("get_message", {"account_id": "personal", "id": item.id}, "owner")
        assert result["reply_to"]["sender_name"] == "Known author 100%_"
        assert result["reply_to"]["sender_jid"] == LID


def test_postgres_unicode_search_and_batch_actor_resolution(postgres_registration):
    from app.identities import prefetch_actor_names, actor_names
    from test_identities import metadata
    application, _ = postgres_registration
    with session(application):
        ingest_identities(link())
        ingest_identities(metadata(name="Élodie"))
        rows = tool("search_contacts", {"account_id": "personal", "query": "ÉLODIE"}, "owner")["items"]
        assert {row["contact_id"] for row in rows} == {PN, LID}
        prefetch_actor_names("personal", [PN, LID, "unknown@lid"])
        assert actor_names("personal", LID, "actor")["actor_name"] == "Élodie"
        assert actor_names("personal", "unknown@lid", "actor")["actor_name"] == "unknown@lid"


def test_postgres_edited_reply_authors_are_known_before_original(postgres_registration):
    from app.message_updates import ingest_update
    from test_message_updates import edit_payload, update_payload
    application, _ = postgres_registration
    with session(application):
        ingest_update(update_payload(kind="edit", actor_jid="editor@lid", chat_jid=GROUP,
            target_message_id="not-yet-received", edit=edit_payload(reply_to={
                "message_id": "missing", "sender_jid": "edited-author@lid"})))
        assert {"edited-author@lid", "editor@lid"} <= set(identity_page("personal")["items"])
        set_alias("personal", "edited-author@lid", "Edited author")
        assert tool("search_contacts", {"account_id": "personal", "query": "Edited author"}, "owner")["items"][0]["contact_id"] == "edited-author@lid"
