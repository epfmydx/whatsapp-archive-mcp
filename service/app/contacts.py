"""Read-only search of locally known identities, with SQL-only candidate selection.

Address variants remain separate results. Only explicit, unambiguous reverse
PN/LID links share names/aliases, exactly as in the display resolver.
"""
import re

from flask import g
from sqlalchemy import Boolean, String, and_, func, literal, or_, select, union, union_all
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from .models import IdentityAlias, IdentityLink, IdentityName, Message, MessageMetadata, MessageReaction, MessageState, MessageUpdate
from .pagination import cursor_context, decode_cursor, page
from .profiles import require_account


class SearchLower(FunctionElement):
    """Unicode lowercasing for name/ID search in both supported dialects."""
    type = String()
    inherit_cache = True


@compiles(SearchLower)
def _search_lower(element, compiler, **kw):
    return "lower("+compiler.process(list(element.clauses)[0], **kw)+")"


@compiles(SearchLower, "sqlite")
def _sqlite_lower(element, compiler, **kw):
    return "wa_lower("+compiler.process(list(element.clauses)[0], **kw)+")"


class ReplyAuthor(FunctionElement):
    """Project one JSON scalar, never a body/envelope into Python."""
    type = String()
    inherit_cache = True


class UpdateActor(FunctionElement):
    type = String()
    inherit_cache = True


def _json_scalar(element, compiler, dialect, path, **kw):
    value = compiler.process(list(element.clauses)[0], **kw)
    if dialect == "sqlite":
        pointer = "$."+".".join(path)
        return (f"CASE WHEN json_valid({value}) THEN CASE WHEN json_type({value}, '{pointer}') = 'text' "
                f"THEN json_extract({value}, '{pointer}') END END")
    pointer = "CAST("+value+" AS JSON)"+"".join("->'"+part+"'" for part in path)
    scalar = "CAST("+value+" AS JSON)"+"".join("->'"+part+"'" for part in path[:-1])+"->>'"+path[-1]+"'"
    return f"CASE WHEN json_typeof({pointer}) = 'string' THEN {scalar} END"


@compiles(ReplyAuthor, "sqlite")
def _sqlite_reply(element, compiler, **kw):
    return _json_scalar(element, compiler, "sqlite", ("reply_to", "sender_jid"), **kw)


@compiles(ReplyAuthor, "postgresql")
def _postgres_reply(element, compiler, **kw):
    return _json_scalar(element, compiler, "postgresql", ("reply_to", "sender_jid"), **kw)


@compiles(UpdateActor, "sqlite")
def _sqlite_actor(element, compiler, **kw):
    return _json_scalar(element, compiler, "sqlite", ("actor_jid",), **kw)


@compiles(UpdateActor, "postgresql")
def _postgres_actor(element, compiler, **kw):
    return _json_scalar(element, compiler, "postgresql", ("actor_jid",), **kw)


# Unicode Cc/Cf ranges rejected by plain_name/identity_jid. Surrogates cannot be
# stored as valid database UTF-8. NUL is checked separately on SQLite; PostgreSQL
# text cannot contain it. Keep the same ranges in both dialects.
_FORBIDDEN = "\x01-\x1f\x7f-\x9f\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f"


class CleanText(FunctionElement):
    type = Boolean()
    inherit_cache = True


def clean_text(column, max_length=200, identity=False):
    forbidden = _FORBIDDEN + (" \u00a0\u1680\u2000-\u200a\u2028-\u2029\u202f\u205f\u3000" if identity else "")
    return CleanText(column, literal("["+forbidden+"]"), literal(max_length))


@compiles(CleanText, "sqlite")
def _sqlite_clean(element, compiler, **kw):
    value, pattern, maximum = [compiler.process(arg, **kw) for arg in element.clauses]
    return f"(length({value}) BETWEEN 1 AND {maximum} AND instr({value}, char(0)) = 0 AND {value} NOT GLOB ('*' || {pattern} || '*'))"


@compiles(CleanText, "postgresql")
def _postgres_clean(element, compiler, **kw):
    value, pattern, maximum = [compiler.process(arg, **kw) for arg in element.clauses]
    return f"(length({value}) BETWEEN 1 AND {maximum} AND {value} !~ {pattern})"


def known_identity_ids(account_id):
    """Include archive references, local names/aliases and explicit links only."""
    sources = ((Message, Message.chat_jid), (Message, Message.sender_jid),
               (IdentityName, IdentityName.jid), (IdentityAlias, IdentityAlias.target_id),
               (IdentityLink, IdentityLink.lid), (IdentityLink, IdentityLink.pn),
               (MessageReaction, MessageReaction.actor_jid),
               (MessageMetadata, ReplyAuthor(MessageMetadata.envelope)),
               (MessageState, ReplyAuthor(MessageState.envelope)),
               (MessageState, ReplyAuthor(MessageState.edit_payload)),
               (MessageUpdate, UpdateActor(MessageUpdate.payload)))
    ids = union(*(select(column.label("jid")).where(model.account_id == account_id)
                  for model, column in sources)).subquery()
    return select(ids.c.jid).where(clean_text(ids.c.jid, 300, identity=True)).cte("known_ids")


def _numeric_address(column, suffix, minimum, maximum, nonzero=False):
    digits = func.substr(column, 1, func.length(column)-len(suffix))
    stripped = digits
    for char in "0123456789":
        stripped = func.replace(stripped, char, "")
    return and_(column.endswith(suffix), func.length(digits).between(minimum, maximum), stripped == "",
                func.substr(digits, 1, 1) != "0" if nonzero else literal(True))


def _searchable_names(account_id, ids):
    """SQL equivalent of current linked metadata and bounded history selection.

    Each current name kind is searchable, even when a higher-precedence alias
    supplies display_name. A latest clear suppresses that kind and its older
    historical fallback. Body text and shared contact cards are never searched.
    """
    from .identities import HISTORICAL_CANDIDATE_LIMIT, NAME_KINDS
    link = IdentityLink.__table__.alias("links")
    counts = select(IdentityLink.pn.label("pn"), func.count().label("n")).where(
        IdentityLink.account_id == account_id).group_by(IdentityLink.pn).cte("link_counts")
    valid_link = and_(_numeric_address(link.c.pn, "@s.whatsapp.net", 5, 15, True),
                      _numeric_address(link.c.lid, "@lid", 1, 30))
    linked = select(ids.c.jid.label("root"), link.c.pn.label("member"), counts.c.n.label("n")).select_from(
        ids.join(link, and_(link.c.account_id == account_id, link.c.lid == ids.c.jid)).join(counts, counts.c.pn == link.c.pn)).where(valid_link)
    reverse = select(ids.c.jid.label("root"), link.c.lid.label("member"), counts.c.n.label("n")).select_from(
        ids.join(link, and_(link.c.account_id == account_id, link.c.pn == ids.c.jid)).join(counts, counts.c.pn == link.c.pn)).where(valid_link, counts.c.n == 1)
    peers = union_all(linked, reverse).cte("peers")
    exact = select(ids.c.jid.label("root"), ids.c.jid.label("member"))
    members = union(exact, select(peers.c.root, peers.c.member)).cte("members")
    alias_members = union(exact, select(peers.c.root, peers.c.member).where(peers.c.n == 1)).cte("alias_members")
    names = select(members.c.root, IdentityName.kind, IdentityName.name,
                   func.row_number().over(partition_by=(members.c.root, IdentityName.kind), order_by=(
                       IdentityName.observed_at.desc(), (IdentityName.name == "").desc(), IdentityName.jid.desc())).label("rank")).select_from(
        members.join(IdentityName, and_(IdentityName.account_id == account_id, IdentityName.jid == members.c.member))).where(
            IdentityName.kind.in_(NAME_KINDS), or_(IdentityName.kind != "push", func.trim(IdentityName.name) != "-")).cte("ranked_names")
    aliases = select(alias_members.c.root, IdentityAlias.alias.label("name"),
                     func.row_number().over(partition_by=(alias_members.c.root, IdentityAlias.target_type), order_by=(IdentityAlias.updated_at.desc(),
                         _numeric_address(IdentityAlias.target_id, "@s.whatsapp.net", 5, 15, True).desc(), IdentityAlias.target_id.desc())).label("rank")).select_from(
        alias_members.join(IdentityAlias, and_(IdentityAlias.account_id == account_id, IdentityAlias.target_id == alias_members.c.member))).where(
            IdentityAlias.target_type.in_(("contact", "chat")), IdentityAlias.alias != "").cte("ranked_aliases")
    def historical(kind, name_column, jid_column):
        clears = select(members.c.root, func.max(IdentityName.observed_at).label("stamp")).select_from(
            members.join(IdentityName, and_(IdentityName.account_id == account_id, IdentityName.jid == members.c.member))).where(
                IdentityName.kind == kind, IdentityName.name == "").group_by(members.c.root).cte(kind+"_clears")
        history = select(members.c.root, name_column.label("name"),
                         func.row_number().over(partition_by=members.c.root, order_by=(Message.timestamp.desc(), Message.id.desc())).label("rank")).select_from(
            members.join(Message, and_(Message.account_id == account_id, jid_column == members.c.member)).outerjoin(clears, clears.c.root == members.c.root)).where(
                func.trim(name_column) != "", func.length(name_column) <= 200,
                or_(clears.c.stamp.is_(None), Message.timestamp > clears.c.stamp))
        if kind == "push":
            history = history.where(func.trim(name_column) != "-")
        history = history.cte(kind+"_history_window")
        valid_history = select(history.c.root, history.c.name,
                               func.row_number().over(partition_by=history.c.root, order_by=history.c.rank).label("rank")).where(
            history.c.rank <= HISTORICAL_CANDIDATE_LIMIT, clean_text(history.c.name)).cte(kind+"_valid_history")
        # A current directory value replaces older observations of that same
        # kind. Clears leave only the newer-than-clear historical window.
        current = select(names.c.root).where(names.c.kind == kind, names.c.rank == 1,
                                             names.c.name != "", clean_text(names.c.name))
        return select(valid_history.c.root, valid_history.c.name).where(
            valid_history.c.rank == 1, ~valid_history.c.root.in_(current))

    return union_all(select(names.c.root, names.c.name).where(names.c.rank == 1, names.c.name != "", clean_text(names.c.name)),
                     select(aliases.c.root, aliases.c.name).where(aliases.c.rank == 1, clean_text(aliases.c.name)),
                     historical("push", Message.sender_name, Message.sender_jid),
                     historical("chat", Message.chat_name, Message.chat_jid)).cte("search_names"), members


def search_contacts(account_id, query, owner, cursor=None, limit=50, include_phone_number=False):
    from .identities import _linked_jids, actor_names, identity_phone, plain_name, prefetch_actor_names
    require_account(account_id, owner)
    if not isinstance(query, str) or not 1 <= len(query) <= 200:
        raise ValueError("query must contain 1..200 characters")
    query = plain_name(query, allow_empty=False)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if type(include_phone_number) is not bool:
        raise ValueError("include_phone_number must be a boolean")
    context = cursor_context(owner, account_id, "search_contacts", {"query": query, "include_phone_number": include_phone_number})
    ids = known_identity_ids(account_id)
    names, members = _searchable_names(account_id, ids)
    match = SearchLower(names.c.name).contains(query.lower(), autoescape=True)
    named = select(names.c.root).where(match)
    id_match = SearchLower(members.c.member).contains(query.lower(), autoescape=True)
    # Normalize only phone-shaped queries; letters are never discarded to
    # invent a numeric match. PN matching remains bound to known identities.
    if re.fullmatch(r"[+0-9\s().-]+", query):
        digits = re.sub(r"[^0-9]", "", query)
        if digits:
            id_match = or_(id_match, and_(_numeric_address(members.c.member, "@s.whatsapp.net", 5, 15, True),
                func.substr(members.c.member, 1, func.length(members.c.member)-15).contains(digits, autoescape=True)))
    statement = select(ids.c.jid).where(~ids.c.jid.endswith("@g.us"), or_(
        ids.c.jid.in_(named), ids.c.jid.in_(select(members.c.root).where(id_match))))
    if cursor is not None:
        statement = statement.where(ids.c.jid > decode_cursor(cursor, context)["id"])
    jids = list(g.db.scalars(statement.order_by(ids.c.jid).limit(limit+1)))
    prefetch_actor_names(account_id, jids)
    items = []
    for jid in jids:
        resolved = actor_names(account_id, jid, "contact")
        item = {"contact_id": jid, "display_name": resolved["contact_name"], "name_source": resolved["contact_name_source"],
                "whatsapp_ids": _linked_jids(account_id, jid)}
        if include_phone_number:
            item["phone_number"] = identity_phone(account_id, jid)
        items.append(item)
    # ID-only pagination reuses the signed archive format with a constant valid
    # timestamp. The position is ordered exclusively by actual contact ID.
    return page(items, limit, context, lambda item: ("1970-01-01T00:00:00.000000Z", item["contact_id"]))
