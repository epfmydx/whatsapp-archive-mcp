"""Account-scoped, local display metadata; never changes the WhatsApp archive."""
import re
import unicodedata
from datetime import datetime, timezone
from functools import wraps

from flask import g, has_request_context, jsonify, request
from sqlalchemy import and_, event, func, literal, or_, select, union_all

from .auth import internal_required
from .models import Account, IdentityAlias, IdentityLink, IdentityName, Message, now
from .profiles import require_account

NAME_KINDS = ("contact_full", "contact_first", "business", "push", "chat")
PN = re.compile(r"([1-9][0-9]{4,14})@s\.whatsapp\.net\Z")
LID = re.compile(r"[0-9]{1,30}@lid\Z")
HISTORICAL_CANDIDATE_LIMIT = 16
CACHE_ENTRY_LIMIT = 2048


def _cache_key(value):
    if isinstance(value, dict):
        return tuple(sorted((str(key), _cache_key(item)) for key, item in value.items()))
    return tuple(_cache_key(item) for item in value) if isinstance(value, (list, tuple)) else value


def _after_flush(session, context):
    if has_request_context() and getattr(g, "db", None) is session:
        _invalidate_cache()


def _cached(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not g.db.info.get("identity_cache_listener"):
            event.listen(g.db, "after_flush", _after_flush)
            g.db.info["identity_cache_listener"] = True
        key = (function.__name__, _cache_key(args), _cache_key(kwargs))
        cache = getattr(g, "identity_cache", None)
        if cache is None:
            cache = g.identity_cache = {}
        if key not in cache:
            value = function(*args, **kwargs)
            if len(cache) >= CACHE_ENTRY_LIMIT:
                cache.clear()
            cache[key] = value
        return cache[key]
    return wrapped


def _invalidate_cache():
    g.identity_cache = {}


def plain_name(value, allow_empty=True):
    if not isinstance(value, str) or len(value) > 200 or any(
            unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        raise ValueError("Invalid identity name")
    value = value.strip()
    if not allow_empty and not value:
        raise ValueError("Enter an alias")
    return value


def identity_jid(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 300 or any(
            char.isspace() or unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        raise ValueError("Invalid identity identifier")
    return value


def observed_timestamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 50:
        raise ValueError("Invalid observation timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise ValueError("Invalid observation timestamp") from None


def _safe_name(value, push=False):
    try:
        name = plain_name(value)
        # WhatsApp uses a single hyphen for an unavailable push name. It is
        # not an observation clearing a saved name, nor a display candidate.
        return "" if push and name == "-" else name
    except ValueError:
        return ""


@_cached
def _linked_jids(account_id, jid):
    # Only explicit links count. A reverse lookup is usable only when it has
    # one exact LID; never merge aliases through an ambiguous reverse mapping.
    if LID.fullmatch(jid):
        link = g.db.get(IdentityLink, (account_id, jid))
        return [jid, link.pn] if link and PN.fullmatch(link.pn) else [jid]
    if PN.fullmatch(jid):
        links = list(g.db.scalars(select(IdentityLink.lid).where(
            IdentityLink.account_id == account_id, IdentityLink.pn == jid).order_by(IdentityLink.lid).limit(2)))
        if len(links) == 1 and LID.fullmatch(links[0]):
            return [jid, links[0]]
    return [jid]


@_cached
def _alias_jids(account_id, jid):
    jids = _linked_jids(account_id, jid)
    if len(jids) == 2 and LID.fullmatch(jid):
        # Multiple LIDs with one PN are not enough evidence to merge manual
        # aliases. Phone display may still use the exact forward observation.
        matches = list(g.db.scalars(select(IdentityLink.lid).where(
            IdentityLink.account_id == account_id, IdentityLink.pn == jids[1]).limit(2)))
        if len(matches) != 1:
            return [jid]
    return jids


def _phone_jid(jids):
    return next((jid for jid in jids if PN.fullmatch(jid)), jids[0])


@_cached
def _alias(account_id, target_type, jids):
    rows = list(g.db.scalars(select(IdentityAlias).where(
        IdentityAlias.account_id == account_id, IdentityAlias.target_type == target_type,
        IdentityAlias.target_id.in_(jids), IdentityAlias.alias != "")))
    return _choose_alias(rows)


def _choose_alias(rows):
    if rows:
        # Unlinked aliases can predate a later explicit link. Prefer the latest
        # manual edit; the canonical PN wins an equal-second tie consistently.
        row = max(rows, key=lambda item: (item.updated_at, bool(PN.fullmatch(item.target_id)), item.target_id))
        return {"name": row.alias, "source": "alias"}
    return None


@_cached
def _directory(account_id, jids, kinds):
    rows = list(g.db.scalars(select(IdentityName).where(
        IdentityName.account_id == account_id, IdentityName.jid.in_(jids), IdentityName.kind.in_(kinds))))
    return _choose_directory(rows, kinds)


def _choose_directory(rows, kinds):
    for kind in kinds:
        candidates = [row for row in rows if row.kind == kind and not (kind == "push" and row.name.strip() == "-")]
        if candidates:
            # A clear applies to this kind across the explicitly linked
            # identity. Include empty observations before choosing the newest;
            # an equal-time clear wins independently of the lookup direction.
            row = max(candidates, key=lambda item: (item.observed_at, item.name == "", item.jid))
            if row.name:
                return {"name": row.name, "source": kind}
    return None


@_cached
def _cleared_at(account_id, jids, target_type):
    kind = "chat" if target_type == "chat" else "push"
    return g.db.scalar(select(func.max(IdentityName.observed_at)).where(
        IdentityName.account_id == account_id, IdentityName.jid.in_(jids),
        IdentityName.kind == kind, IdentityName.name == ""))


@_cached
def _historical(account_id, jids, target_type):
    cleared = _cleared_at(account_id, jids, target_type)
    name_column = Message.chat_name if target_type == "chat" else Message.sender_name
    jid_column = Message.chat_jid if target_type == "chat" else Message.sender_jid
    # Project only display metadata, never archive bodies/media. A bounded
    # candidate window tolerates malformed historical names without an
    # unbounded Python scan; exhausted windows fall back to the identifier.
    query = select(name_column, Message.timestamp, Message.id).where(
        Message.account_id == account_id, jid_column.in_(jids),
        func.trim(name_column) != "", func.length(name_column) <= 200)
    if target_type == "contact":
        query = query.where(func.trim(name_column) != "-")
    if cleared:
        query = query.where(Message.timestamp > cleared)
    query = query.order_by(Message.timestamp.desc(), Message.id.desc()).limit(HISTORICAL_CANDIDATE_LIMIT)
    for name, timestamp, ident in g.db.execute(query):
        value = _safe_name(name, push=target_type == "contact")
        if value:
            return {"name": value, "source": "observed"}
    return None


def _fallback(jid):
    match = PN.fullmatch(jid)
    return {"name": "+"+match.group(1), "source": "phone"} if match else {"name": jid, "source": "jid"}


@_cached
def resolve_identity(account_id, jid, target_type="contact", observed_name=""):
    require_account(account_id)
    jid = identity_jid(jid)
    if target_type not in ("contact", "chat"):
        raise ValueError("Invalid identity target")
    if target_type == "chat":
        return resolve_chat_name(account_id, jid, observed_name)
    return _contact_result(account_id, jid, observed_name)


def _contact_result(account_id, jid, observed_name=""):
    jids = _linked_jids(account_id, jid)
    result = (_alias(account_id, "contact", _alias_jids(account_id, jid))
              or _directory(account_id, jids, NAME_KINDS[:-1])
              or _historical(account_id, jids, "contact"))
    if result:
        return result
    value = _safe_name(observed_name, push=True) if not _cleared_at(account_id, jids, "contact") else ""
    return {"name": value, "source": "observed"} if value else _fallback(_phone_jid(jids))


@_cached
def resolve_chat_name(account_id, jid, observed_name=""):
    require_account(account_id)
    jid = identity_jid(jid)
    return _chat_result(account_id, jid, observed_name)


def _chat_result(account_id, jid, observed_name=""):
    jids = _linked_jids(account_id, jid)
    result = _alias(account_id, "chat", _alias_jids(account_id, jid))
    group = jid.endswith("@g.us")
    if not group:
        result = result or _alias(account_id, "contact", _alias_jids(account_id, jid)) or _directory(account_id, jids, NAME_KINDS[:-1])
    result = result or _directory(account_id, jids, ("chat",))
    if not group:
        # A direct-chat peer may have an incoming push name. Never substitute
        # the outgoing message sender (the owner's identity) for that peer.
        result = result or _historical(account_id, jids, "contact")
    result = result or _historical(account_id, jids, "chat")
    if result:
        return result
    value = _safe_name(observed_name) if not _cleared_at(account_id, jids, "chat") else ""
    return {"name": value, "source": "observed"} if value else _fallback(_phone_jid(jids))


@_cached
def identity_phone(account_id, jid):
    require_account(account_id)
    jid = identity_jid(jid)
    match = PN.fullmatch(_phone_jid(_linked_jids(account_id, jid)))
    return "+"+match.group(1) if match else None


def prefetch_actor_names(account_id, identifiers):
    """Warm existing resolver caches with bounded metadata-only batch queries.

    Each caller passes one message's visible actors (at most 100 reactions plus
    its quoted author). Historical windows match _historical: filter clears and
    placeholders first, then inspect at most 16 rows per identity and kind.
    """
    require_account(account_id)
    roots = set()
    for jid in identifiers:
        try:
            roots.add(identity_jid(jid))
        except ValueError:
            continue
    if not roots:
        return
    cache = getattr(g, "identity_cache", None)
    if cache is None:
        cache = g.identity_cache = {}

    def cached(name, *args):
        return (name, _cache_key(args), ())

    # Reserve room before warming a batch, so the general cache cap cannot
    # evict its earlier actors halfway through rendering the same message.
    if len(cache) + len(roots)*14 >= CACHE_ENTRY_LIMIT:
        cache.clear()
    roots = sorted(jid for jid in roots if cached("resolve_identity", account_id, jid) not in cache
                   or cached("resolve_chat_name", account_id, jid) not in cache)
    if not roots:
        return
    direct = list(g.db.scalars(select(IdentityLink).where(
        IdentityLink.account_id == account_id, IdentityLink.lid.in_(roots))))
    forwards = {row.lid: row.pn for row in direct}
    phones = {jid for jid in roots if PN.fullmatch(jid)} | {row.pn for row in direct if PN.fullmatch(row.pn)}
    reverse = {}
    if phones:
        ranked = select(IdentityLink.pn, IdentityLink.lid, func.row_number().over(
            partition_by=IdentityLink.pn, order_by=IdentityLink.lid).label("rank")).where(
                IdentityLink.account_id == account_id, IdentityLink.pn.in_(phones)).subquery()
        for pn, lid in g.db.execute(select(ranked.c.pn, ranked.c.lid).where(ranked.c.rank <= 2)):
            reverse.setdefault(pn, []).append(lid)
    members, alias_members = {}, {}
    for jid in roots:
        linked = [jid]
        if LID.fullmatch(jid) and PN.fullmatch(forwards.get(jid, "")):
            linked.append(forwards[jid])
        elif PN.fullmatch(jid) and len(reverse.get(jid, [])) == 1 and LID.fullmatch(reverse[jid][0]):
            linked.append(reverse[jid][0])
        members[jid] = linked
        aliases = linked if not (len(linked) == 2 and LID.fullmatch(jid) and len(reverse.get(linked[1], [])) != 1) else [jid]
        alias_members[jid] = aliases
        cache[cached("_linked_jids", account_id, jid)] = linked
        cache[cached("_alias_jids", account_id, jid)] = aliases
    all_members = {member for linked in members.values() for member in linked}
    names = list(g.db.scalars(select(IdentityName).where(IdentityName.account_id == account_id, IdentityName.jid.in_(all_members))))
    aliases = list(g.db.scalars(select(IdentityAlias).where(IdentityAlias.account_id == account_id, IdentityAlias.target_id.in_(all_members))))
    cleared = {}
    for jid in roots:
        rows = [row for row in names if row.jid in members[jid]]
        for kinds in (NAME_KINDS[:-1], ("chat",)):
            cache[cached("_directory", account_id, members[jid], kinds)] = _choose_directory(rows, kinds)
        for target, kind in (("contact", "push"), ("chat", "chat")):
            cleared[jid, target] = max((row.observed_at for row in rows if row.kind == kind and row.name == ""), default=None)
            cache[cached("_cleared_at", account_id, members[jid], target)] = cleared[jid, target]
            cache[cached("_alias", account_id, target, alias_members[jid])] = _choose_alias([
                row for row in aliases if row.target_type == target and row.target_id in alias_members[jid] and row.alias != ""])
    # SQL returns metadata only. The literals form a bounded per-message root to
    # linked-ID relation; no message/envelope/body objects are materialized.
    relation = union_all(*(select(literal(jid).label("root"), literal(member).label("member"),
                                  literal(cleared[jid, "contact"], type_=Message.timestamp.type).label("contact_clear"),
                                  literal(cleared[jid, "chat"], type_=Message.timestamp.type).label("chat_clear"))
                           for jid in roots for member in members[jid])).cte("actor_members")
    for target, column, jid_column, cutoff in (
            ("contact", Message.sender_name, Message.sender_jid, relation.c.contact_clear),
            ("chat", Message.chat_name, Message.chat_jid, relation.c.chat_clear)):
        query = select(relation.c.root, column.label("name"), func.row_number().over(
            partition_by=relation.c.root, order_by=(Message.timestamp.desc(), Message.id.desc())).label("rank")).select_from(
                relation.join(Message, and_(Message.account_id == account_id, jid_column == relation.c.member))).where(
                    func.trim(column) != "", func.length(column) <= 200, or_(cutoff.is_(None), Message.timestamp > cutoff))
        if target == "contact":
            query = query.where(func.trim(column) != "-")
        window = query.subquery()
        chosen = {}
        for jid, name in g.db.execute(select(window.c.root, window.c.name).where(
                window.c.rank <= HISTORICAL_CANDIDATE_LIMIT).order_by(window.c.root, window.c.rank)):
            value = _safe_name(name, push=target == "contact")
            if value and jid not in chosen:
                chosen[jid] = {"name": value, "source": "observed"}
        for jid in roots:
            cache[cached("_historical", account_id, members[jid], target)] = chosen.get(jid)
    for jid in roots:
        cache[cached("resolve_identity", account_id, jid)] = _contact_result(account_id, jid)
        cache[cached("resolve_chat_name", account_id, jid)] = _chat_result(account_id, jid)
        match = PN.fullmatch(_phone_jid(members[jid]))
        cache[cached("identity_phone", account_id, jid)] = "+"+match.group(1) if match else None


def actor_names(account_id, jid, prefix):
    """Legacy actor fields may be absent or malformed; preserve the raw field."""
    try:
        resolved = resolve_identity(account_id, jid)
        if resolved["source"] in ("phone", "jid") and not jid.endswith("@g.us"):
            chat = resolve_chat_name(account_id, jid)
            if chat["source"] not in ("phone", "jid"):
                resolved = chat
        phone = identity_phone(account_id, jid)
    except ValueError:
        resolved, phone = {"name": None, "source": "unknown"}, None
    return {prefix+"_name": resolved["name"], prefix+"_name_source": resolved["source"],
            prefix+"_phone_number": phone}


def resolve_message_names(message):
    chat = resolve_chat_name(message.account_id, message.chat_jid)
    return {**actor_names(message.account_id, message.sender_jid, "sender"),
            "chat_name": chat["name"], "chat_name_source": chat["source"],
            "chat_phone_number": identity_phone(message.account_id, message.chat_jid)}


def _known_target(account_id, target_id, target_type):
    require_account(account_id)
    target_id = identity_jid(target_id)
    if target_type not in ("contact", "chat"):
        raise ValueError("Invalid identity target")
    if target_type == "contact" and target_id.endswith("@g.us"):
        raise ValueError("Use a chat alias for groups")
    from .contacts import known_identity_ids
    directory = known_identity_ids(account_id)
    if not g.db.scalar(select(directory.c.jid).where(directory.c.jid == target_id).limit(1)):
        raise ValueError("Identity not found")
    return target_id


def serialize_account(account_id):
    require_account(account_id)
    # PostgreSQL FK inserts hold KEY SHARE on Account. NO KEY UPDATE serializes
    # our per-account writes without a conflicting lock upgrade after ingest.
    return g.db.get(Account, account_id, with_for_update={"key_share": True}, populate_existing=True)


def set_alias(account_id, target_id, alias, target_type="contact"):
    target_id = _known_target(account_id, target_id, target_type)
    alias = plain_name(alias, allow_empty=False)
    serialize_account(account_id)
    _invalidate_cache()
    jids = _alias_jids(account_id, target_id)
    canonical = _phone_jid(jids)
    rows = list(g.db.scalars(select(IdentityAlias).where(
        IdentityAlias.account_id == account_id, IdentityAlias.target_type == target_type,
        IdentityAlias.target_id.in_(jids)).execution_options(populate_existing=True)))
    row = next((item for item in rows if item.target_id == canonical), None)
    for item in rows:
        if item is not row:
            g.db.delete(item)
    if row is None:
        row = IdentityAlias(account_id=account_id, target_type=target_type, target_id=canonical)
        g.db.add(row)
    row.alias, row.updated_at = alias, now()
    g.db.flush()
    _invalidate_cache()
    return resolve_identity(account_id, target_id, target_type)


def remove_alias(account_id, target_id, target_type="contact"):
    target_id = _known_target(account_id, target_id, target_type)
    serialize_account(account_id)
    _invalidate_cache()
    jids = _alias_jids(account_id, target_id)
    for row in g.db.scalars(select(IdentityAlias).where(
            IdentityAlias.account_id == account_id, IdentityAlias.target_type == target_type,
            IdentityAlias.target_id.in_(jids)).execution_options(populate_existing=True)):
        g.db.delete(row)
    g.db.flush()
    _invalidate_cache()
    return resolve_identity(account_id, target_id, target_type)


def ingest_identities(data):
    if not isinstance(data, dict) or set(data) - {"account_id", "names", "links"}:
        raise ValueError("Invalid identity metadata")
    account = require_account(data.get("account_id"))
    names, links = data.get("names", []), data.get("links", [])
    if not isinstance(names, list) or not isinstance(links, list) or len(names)+len(links) > 100:
        raise ValueError("Identity batch must contain at most 100 entries")
    valid_names, valid_links = [], []
    # Validate the complete batch before writing: normal 400 responses commit
    # in the application, so partial writes on validation failure are unsafe.
    for item in names:
        if not isinstance(item, dict) or set(item) != {"jid", "kind", "name", "observed_at"} or item["kind"] not in NAME_KINDS:
            raise ValueError("Invalid identity name metadata")
        jid, name, timestamp = identity_jid(item["jid"]), plain_name(item["name"]), observed_timestamp(item["observed_at"])
        if item["kind"] == "push" and name == "-":
            continue
        valid_names.append((jid, item["kind"], name, timestamp))
    for item in links:
        if not isinstance(item, dict) or set(item) != {"lid", "pn", "observed_at"}:
            raise ValueError("Invalid identity mapping")
        lid, pn = identity_jid(item["lid"]), identity_jid(item["pn"])
        if not LID.fullmatch(lid) or not PN.fullmatch(pn):
            raise ValueError("Invalid identity mapping")
        valid_links.append((lid, pn, observed_timestamp(item["observed_at"])))
    # Serialize metadata updates for this assigned profile, including inserts.
    serialize_account(account.id)
    for jid, kind, name, timestamp in valid_names:
        row = g.db.get(IdentityName, (account.id, jid, kind), populate_existing=True)
        if row is None:
            g.db.add(IdentityName(account_id=account.id, jid=jid, kind=kind, name=name, observed_at=timestamp))
        elif timestamp > row.observed_at:
            row.name, row.observed_at = name, timestamp
        elif timestamp == row.observed_at and name != row.name:
            # A clearing observation wins equal-time conflicts. Otherwise keep
            # a stable tie break, independent of batch ordering.
            row.name = min(row.name, name)
        g.db.flush()
    for lid, pn, timestamp in valid_links:
        row = g.db.get(IdentityLink, (account.id, lid), populate_existing=True)
        if row is None:
            g.db.add(IdentityLink(account_id=account.id, lid=lid, pn=pn, observed_at=timestamp))
        elif timestamp > row.observed_at:
            row.pn, row.observed_at = pn, timestamp
        elif timestamp == row.observed_at and pn != row.pn:
            row.pn = ""
        g.db.flush()
    _invalidate_cache()
    return {"ok": True}


def identity_page(account_id, after=None, limit=100):
    require_account(account_id)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if after is not None:
        identity_jid(after)
    from .contacts import known_identity_ids
    directory = known_identity_ids(account_id)
    query = select(directory.c.jid)
    if after is not None:
        query = query.where(directory.c.jid > after)
    items = list(g.db.scalars(query.order_by(directory.c.jid).limit(limit+1)))
    more = len(items) > limit
    items = items[:limit]
    return {"items": items, "next_after": items[-1] if more else None, "has_more": more}


def install_identity_routes(app):
    @app.post("/internal/identities")
    @internal_required
    def identities_ingest():
        return jsonify(ingest_identities(request.get_json()))

    @app.get("/internal/accounts/<account_id>/identities")
    @internal_required
    def identities_list(account_id):
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            raise ValueError("Invalid identity page limit") from None
        return jsonify(identity_page(account_id, request.args.get("after"), limit))
