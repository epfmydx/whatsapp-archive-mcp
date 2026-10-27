# Contributing

This project is intentionally a read-only WhatsApp MCP. Requests or patches adding
send/reply/reaction/delete/mark-read/contact-write tools are outside its scope.
The same applies to local archive mutation tools exposed through MCP. Admin
profile/access management and internal protocol handling are separate boundaries.

Use synthetic data in tests; never submit phonebooks, private chats, media, session
files or credentials. Test changed behavior and update its guide. Preserve native
forms, keyboard access, account separation, private storage and bounded queues.
Dependencies and copied material must have compatible licenses with attribution.
Contributions to project code are under GPL-3.0-only; third-party notices remain.
Do not modify upstream brand artwork or imply a fork is endorsed by the brand.

Service: `PYTHONPATH=service python -m pytest service/tests -q` after installing
`service/requirements.txt`. Worker: `python -m unittest discover -s worker/tests`.
Bridge: `cd bridge && go test ./... && go vet ./...` with the declared Go version.
Chart: `python -m pytest charts/whatsapp-mcp/tests -q` and
`helm lint --strict charts/whatsapp-mcp`. Optional PostgreSQL fixtures require a
disposable database; never point them at production or a paired session database.
