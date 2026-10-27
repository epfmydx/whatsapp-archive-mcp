"""Receive-only typed messages and deterministic archive views.

Message and MessageMetadata retain originals for operator recovery. MCP reads
only the effective state; a tombstone always wins, even over later edits.
"""
import json
import math
import re
from datetime import datetime, timezone

from flask import g
from sqlalchemy import select

from .events import emit
from .identities import actor_names, prefetch_actor_names, serialize_account
from .models import Message, MessageMetadata, MessageReaction, MessageState, MessageUpdate
from .profiles import require_account

MESSAGE_TYPES = frozenset(("text", "image", "video", "video_note", "audio", "document", "sticker", "contact", "contacts",
                           "location", "live_location", "poll", "interactive", "unknown"))
ENVELOPE_FIELDS = {"message_type", "text", "content", "reply_to", "is_forwarded", "forwarding_score", "content_truncated"}
REACTION_LIMIT = 100
PREVIEW_LIMIT = 512


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value)-set(allowed) or set(required)-set(value):
        raise ValueError("Invalid message fields")
    return value


def _string(value, limit, empty=True):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value)
            or "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError("Invalid message string")
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid message integer")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("Invalid message boolean")
    return value


def _type(value):
    if not isinstance(value, str) or value not in MESSAGE_TYPES:
        raise ValueError("Invalid message type")
    return value


def update_timestamp(value):
    _string(value, 50, False)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise ValueError("Invalid update timestamp") from None


def typed_content(kind, value):
    fields = {
        "unknown": {"reason", "message_fields", "recovery"},
        "contact": {"contacts"}, "contacts": {"contacts"},
        "location": {"latitude", "longitude", "name", "address"},
        "live_location": {"latitude", "longitude", "name", "address"},
        "poll": {"name", "options", "selectable_count"},
        "interactive": {"kind", "text", "selected_id"},
    }.get(kind, set())
    value = _object(value, fields)
    result = dict(value)
    if kind == "unknown":
        if "reason" in value and value["reason"] not in (
                "unsupported_message_type", "empty_message", "invalid_content", "wrapper_limit"):
            raise ValueError("Invalid unavailable-content reason")
        if "recovery" in value and value["recovery"] != "operator_retained_payload":
            raise ValueError("Invalid unavailable-content recovery")
        if "message_fields" in value:
            names = value["message_fields"]
            if (not isinstance(names, list) or len(names) > 32
                    or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) for name in names)
                    or len(set(names)) != len(names)):
                raise ValueError("Invalid message field names")
    if "contacts" in value:
        contacts = value["contacts"]
        if not isinstance(contacts, list) or len(contacts) > 20:
            raise ValueError("Invalid contacts")
        result["contacts"] = []
        for item in contacts:
            _object(item, {"name", "vcard"})
            result["contacts"].append({key: _string(text, 200 if key == "name" else 8192) for key, text in item.items()})
    for key, bound in (("latitude", 90), ("longitude", 180)):
        if key in value:
            number = value[key]
            if type(number) not in (int, float) or not -bound <= number <= bound or not math.isfinite(number):
                raise ValueError("Invalid coordinates")
    for key in ("name", "address", "kind", "text", "selected_id"):
        if key in value:
            _string(value[key], {"kind": 100, "text": 10000, "address": 2000}.get(key, 1000))
    if "options" in value:
        if not isinstance(value["options"], list) or len(value["options"]) > 100:
            raise ValueError("Invalid poll options")
        for option in value["options"]:
            _string(option, 1000)
    if "selectable_count" in value:
        _integer(value["selectable_count"], 0, 100)
    return result


def reply_data(value, chat_jid):
    _object(value, {"message_id", "chat_jid", "sender_jid", "text", "message_type"}, {"message_id"})
    result = {"message_id": _string(value["message_id"], 300, False), "chat_jid": chat_jid}
    if "chat_jid" in value and value["chat_jid"] != chat_jid:
        raise ValueError("Reply must reference the same chat")
    for key, limit in (("sender_jid", 300), ("text", 4096)):
        if key in value:
            result[key] = _string(value[key], limit)
    if "message_type" in value:
        result["message_type"] = _type(value["message_type"])
    return result


def infer_type(media, text):
    if media:
        mime = media.get("mime_type", "")
        if mime.startswith("image/"): return "image"
        if mime.startswith("video/"): return "video"
        if mime.startswith("audio/"): return "audio"
        return "document"
    return "text" if text else "unknown"


def envelope(data, chat_jid, media=None, partial=False):
    result = {}
    if "message_type" in data:
        result["message_type"] = _type(data["message_type"])
    elif not partial:
        result["message_type"] = infer_type(media, data.get("text", ""))
    if "text" in data or not partial:
        result["text"] = _string(data.get("text", ""), 65536)
    if "content" in data or not partial:
        # Partial edits with content must name its type to validate fields.
        result["content"] = typed_content(result.get("message_type", "unknown"), data.get("content", {}))
    if "reply_to" in data:
        result["reply_to"] = reply_data(data["reply_to"], chat_jid) if data["reply_to"] is not None else None
    elif not partial:
        result["reply_to"] = None
    if "is_forwarded" in data or not partial:
        result["is_forwarded"] = _boolean(data.get("is_forwarded", False))
    if "forwarding_score" in data or not partial:
        result["forwarding_score"] = _integer(data.get("forwarding_score", 0), 0, 2**31-1)
    if "content_truncated" in data or not partial:
        result["content_truncated"] = _boolean(data.get("content_truncated", False))
    return result


def key_for(message):
    return message.account_id, message.chat_jid, message.message_id


def state_for(key, create=False):
    state = g.db.get(MessageState, key)
    if state is None and create:
        state = MessageState(account_id=key[0], chat_jid=key[1], message_id=key[2])
        g.db.add(state)
    return state


def original_envelope(message):
    metadata = g.db.get(MessageMetadata, key_for(message))
    return json.loads(metadata.envelope) if metadata else envelope(
        {"text": message.text}, message.chat_jid, json.loads(message.media) if message.media else None)


def refresh_state(message, state):
    original = original_envelope(message)
    effective = original | (json.loads(state.edit_payload) if state.edit_payload else {})
    # Original media is kept independently, including on caption edits.
    state.effective_text = effective["text"]
    state.envelope = json.dumps(effective)


def record_original(message, original):
    key = key_for(message)
    g.db.add(MessageMetadata(account_id=key[0], chat_jid=key[1], message_id=key[2], envelope=json.dumps(original)))
    g.db.flush()
    refresh_state(message, state_for(key, create=True))


def ingest_update(data):
    _object(data, {"account_id", "event_id", "chat_jid", "target_message_id", "actor_jid", "actor_is_from_me",
                   "kind", "timestamp", "reaction", "edit", "target_sender_jid", "target_is_from_me"},
            {"account_id", "event_id", "chat_jid", "target_message_id", "actor_jid", "actor_is_from_me", "kind", "timestamp"})
    account = require_account(data["account_id"])
    for key in ("chat_jid", "target_message_id", "actor_jid"):
        _string(data[key], 300, False)
    if not isinstance(data["event_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["event_id"]):
        raise ValueError("Invalid update event ID")
    _boolean(data["actor_is_from_me"])
    if "target_sender_jid" in data: _string(data["target_sender_jid"], 300, False)
    if "target_is_from_me" in data: _boolean(data["target_is_from_me"])
    kind = data["kind"]
    if kind not in ("reaction", "edit", "delete"):
        raise ValueError("Invalid update kind")
    stamp = update_timestamp(data["timestamp"])
    edit = None
    if kind == "reaction":
        if "edit" in data: raise ValueError("Invalid update fields")
        reaction = _object(data.get("reaction"), {"emoji"}, {"emoji"})
        emoji = _string(reaction["emoji"], 64)
    elif kind == "edit":
        if "reaction" in data: raise ValueError("Invalid update fields")
        # Complete snapshots make event-time ordering independent of delivery
        # order; a later text edit cannot accidentally revert earlier metadata.
        _object(data.get("edit"), ENVELOPE_FIELDS, ENVELOPE_FIELDS)
        edit = envelope(data["edit"], data["chat_jid"], partial=True)
    elif "reaction" in data or "edit" in data:
        raise ValueError("Invalid update fields")
    serialize_account(account.id)
    event_key = (account.id, data["event_id"])
    if g.db.get(MessageUpdate, event_key):
        return {"duplicate": True, "event_id": data["event_id"]}
    key = account.id, data["chat_jid"], data["target_message_id"]
    state = state_for(key, create=True)
    order = stamp, data["event_id"]
    if kind == "reaction":
        reaction_key = (*key, data["actor_jid"])
        row = g.db.get(MessageReaction, reaction_key)
        if row is None:
            row = MessageReaction(account_id=key[0], chat_jid=key[1], message_id=key[2], actor_jid=data["actor_jid"])
            g.db.add(row)
        if row.timestamp is None or order > (row.timestamp, row.event_id):
            row.emoji, row.timestamp, row.event_id = emoji, stamp, data["event_id"]
            row.actor_is_from_me = data["actor_is_from_me"]
    elif kind == "edit" and (state.edited_at is None or order > (state.edited_at, state.edit_event_id)):
        state.edit_payload, state.edited_at, state.edit_event_id = json.dumps(edit), stamp, data["event_id"]
    elif kind == "delete" and (state.deleted_at is None or order > (state.deleted_at, state.delete_event_id)):
        state.deleted_at, state.delete_event_id = stamp, data["event_id"]
    message = g.db.scalar(select(Message).where(Message.account_id == key[0], Message.chat_jid == key[1], Message.message_id == key[2]))
    if message:
        refresh_state(message, state)
    g.db.add(MessageUpdate(account_id=key[0], event_id=data["event_id"], chat_jid=key[1], message_id=key[2],
                           kind=kind, payload=json.dumps(data)))
    payload = {"account_id": key[0], "chat_jid": key[1], "message_id": key[2], "event_id": data["event_id"], "kind": kind}
    if message: payload["id"] = message.id
    emit(g.db, key[0], "whatsapp.message.reaction" if kind == "reaction" else "whatsapp.message.updated", payload)
    return {"duplicate": False, "event_id": data["event_id"]}


def is_deleted(message):
    state = state_for(key_for(message))
    return state is not None and state.deleted_at is not None


def effective_envelope(message, state=None):
    state = state if state is not None else state_for(key_for(message))
    return json.loads(state.envelope) if state and state.envelope else original_envelope(message)


def reply_preview(account_id, chat_jid, reply):
    if not reply: return None
    # Never follow a supplied cross-chat key, including older stored envelopes.
    if reply.get("chat_jid", chat_jid) != chat_jid: return None
    key = account_id, chat_jid, reply["message_id"]
    state = state_for(key)
    target = g.db.scalar(select(Message).where(Message.account_id == account_id, Message.chat_jid == chat_jid,
                                               Message.message_id == key[2]))
    deleted = bool(state and state.deleted_at)
    result = {"message_id": key[2], "chat_jid": chat_jid, "resolved": target is not None, "is_deleted": deleted}
    if deleted:
        return result | {"text": "", "message_type": "unknown", "text_truncated": False}
    if target:
        info = effective_envelope(target, state)
        result.update(id=target.id, sender_jid=target.sender_jid)
    else:
        # An edit can precede its original. Prefer the received edit snapshot to
        # a stale quoted fallback even while the target remains unresolved.
        info = json.loads(state.edit_payload) if state and state.edit_payload else reply
        if "sender_jid" in reply: result["sender_jid"] = reply["sender_jid"]
    prefetch_actor_names(account_id, [result.get("sender_jid")])
    result.update(actor_names(account_id, result.get("sender_jid"), "sender"))
    text = info.get("text", "")
    return result | {"text": text[:PREVIEW_LIMIT], "message_type": info.get("message_type", "unknown"),
                     "text_truncated": len(text) > PREVIEW_LIMIT}


def message_view(message):
    state = state_for(key_for(message))
    deleted = bool(state and state.deleted_at)
    info = effective_envelope(message, state)
    result = {**info, "is_edited": bool(state and state.edited_at), "edited_at": state.edited_at if state else None,
              "is_deleted": deleted, "deleted_at": state.deleted_at if state else None}
    if deleted:
        return result | {"text": "", "content": {}, "reply_to": None, "reactions": [], "reactions_truncated": False}
    if info.get("message_type") == "unknown" and not info.get("content", {}).get("reason"):
        # Explain old projections without inventing a decoder outcome or
        # claiming that their already-consumed protobufs were retained.
        result["content"] = {**info.get("content", {}), "reason": "unrecognized_legacy_content", "recovery": "unknown"}
    result["reply_to"] = reply_preview(message.account_id, message.chat_jid, info.get("reply_to"))
    reactions = list(g.db.scalars(select(MessageReaction).where(
        MessageReaction.account_id == message.account_id, MessageReaction.chat_jid == message.chat_jid,
        MessageReaction.message_id == message.message_id, MessageReaction.emoji != "").order_by(
        MessageReaction.timestamp.desc(), MessageReaction.event_id.desc(), MessageReaction.actor_jid).limit(REACTION_LIMIT+1)))
    prefetch_actor_names(message.account_id, [row.actor_jid for row in reactions[:REACTION_LIMIT]])
    result["reactions"] = [{"actor_jid": row.actor_jid, "actor_is_from_me": row.actor_is_from_me,
                            "emoji": row.emoji, "timestamp": row.timestamp,
                            **actor_names(message.account_id, row.actor_jid, "actor")} for row in reactions[:REACTION_LIMIT]]
    result["reactions_truncated"] = len(reactions) > REACTION_LIMIT
    return result
