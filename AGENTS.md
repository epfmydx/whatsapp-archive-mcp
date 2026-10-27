# WhatsApp Archive MCP

Read IMPLEMENTATION.md and docs/README.md before behavior changes. This edition is deliberately read-only through MCP. Never expose message/media sending, reactions, deletions, mark-read, replies, alias mutations or other write tools. Profile management and OAuth/events control-plane operations are explicit admin/protocol functions; they are not WhatsApp write tools. Bounded own-phone media recovery remains internal and must be documented accurately.

Do not copy runtime secrets, archives, sessions, private deployment records or Git history from another checkout. This directory is a separate source snapshot. The owner authorized the first source commit and normal push to `git@github.com:epfmydx/whatsapp-archive-mcp.git` after selecting GPL-3.0-only. This does not authorize release tags, registry/image/model publishing or deployment. Do not alter the sibling private checkout or live deployments.

Use synthetic isolated tests only. Keep source/test/docs changes scoped and use disjoint worker writes.
