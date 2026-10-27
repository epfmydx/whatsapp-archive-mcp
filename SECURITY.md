# Security policy

Do not post credentials, session databases, tokens, pairing codes, signed URLs,
message content or media in a public issue. Use GitHub's [private vulnerability report](https://github.com/epfmydx/whatsapp-archive-mcp/security/advisories/new)
when reporting is enabled for this repository. If that form is unavailable, open
an issue requesting a private contact method without including vulnerability
details or private data.

The connected publishing tools cannot configure or verify the private-reporting
setting. Maintainers should enable GitHub private vulnerability reporting in the
repository security settings. Do not assume a submitted public issue is private.
Security fixes are reviewed against supported releases; there is no promised
response time or independent certification.

Preserve the single-owner boundary, CSRF/PKCE, profile/account scoping, revocation,
private storage, bounded media processing and sanitized logs. Never add WhatsApp
write tools or expose operator recovery credentials through MCP. Use synthetic
fixtures and report exact versions with a minimal private reproduction.
