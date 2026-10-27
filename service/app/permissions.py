"""Only the read scope is supported by the public MCP edition."""
READ_SCOPE = "whatsapp:read"
SUPPORTED_SCOPES = (READ_SCOPE,)


def normalize_scope(value, default_read=True):
    if value in (None, ""):
        return READ_SCOPE if default_read else ""
    if not isinstance(value, str) or len(value) > 80:
        return ""
    parts = value.split(" ")
    if any(not part or part not in SUPPORTED_SCOPES for part in parts) or len(set(parts)) != len(parts):
        return ""
    if READ_SCOPE not in parts:
        return ""
    return " ".join(scope for scope in SUPPORTED_SCOPES if scope in parts)


def has_scope(value, required):
    normalized = normalize_scope(value, default_read=False)
    return bool(normalized and required in normalized.split(" "))
