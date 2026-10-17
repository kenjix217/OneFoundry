This directory is private plugin **source**, not a connected mail plugin.
The Python runtime stays in OneFoundry and runs as a separate process.

No `mcp.json` is included because a cloud-reachable endpoint has not been
approved and verified. A localhost command or package upload cannot connect
cloud Dotty to this service. After HTTPS/OAuth discovery and read-only tool
verification, use `scripts.package_mail_plugin --verified-mcp-url` to produce
the connection package. The package contains no credentials or auth headers.

See `docs/mail-plugin.md` in the OneFoundry repository for the secure handoff.
