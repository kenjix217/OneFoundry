# Installed setup and worker configuration

Chief inspects the selected specialist with `onefoundry.inspect_capability` and
uses `onefoundry.propose_capability_setup` for implemented tools lacking access.
Requests name profiles, existing tool IDs, the source revision, a verification
file and the original task checkpoint. Approval compares the saved source/profile
state before granting anything. Repository permissions cover registered sources
in that project. Rejection grants nothing. Verification calls the real reader;
failure resumes the task with the observed blocker, never a success claim.

If a host checkout is not mounted, the separately configured release worker may
publish a named repository offer. Its optional `setup` configuration contains
`repositories` (each has `alias`, canonical regular Git `path`, and project UUIDs),
`git_timeout_seconds` (1–30), and `restart_timeout_seconds` (1–600). No model supplies
an absolute host path. Chief selects `repository_alias`, exact advertised revision,
a verification file, and intended repository-tool profiles. Approval includes a
read-only mount and brief app restart. The worker rechecks the path identity and
commit, acquires the global maintenance lease, preserves existing source mounts,
restarts only the app, and registers the source through the Gateway. Unknown
effects retain the lease and are not replayed automatically.

Both worker configuration files are owned mode-0600 JSON. They have no environment
override hierarchy: explicit validated JSON is authoritative. Release worker
fields are `spool`, exact loopback `origin`, `preparation`,
`health_timeout_seconds` (1–600), `poll_seconds` (0.1–10),
`capability_refresh_seconds` (1–10), and optional `setup`. `preparation` contains
canonical, disjoint `install_home`, `archive_root`, `source_root`, plus
`build_timeout_seconds` (60–7200), and optional `managed_source_alias`.
The alias opts in only the dedicated clean clone beneath `source_root`. Packaging
adds the exact source advancement and rollback effect to the installation approval.
Activation reconstructs the QA-tested candidate, preserves the old commit, and
advances that clone only with the installed release. Failed verification restores
both release and source. Dirty, changed or mismatched checkouts stop activation. Worker observations expire under the existing
external-code capability age setting; an enabled policy flag is insufficient.

The updater admits a private `data/config/maintenance-transport.compose.json`
overlay containing only the app's shared group and a bind to
`/runner-spool/external`. The host spool and jobs/claims/receipts/artifacts
directories must be owned by the operator with that group and mode 2770. The
updater validates this overlay and checkpoints both the host evidence and private
configuration alongside volumes. Coding children mount neither storage location.

Public search can use a separately installed private SearXNG service with network
alias `searxng`, JSON output enabled, and the existing
`PUBLIC_WEB_SEARCH_ENDPOINT=http://searxng:8080/search` setting. No host port is
needed. Pin its image digest and retain its private configuration in the installed
configuration directory. SearXNG is a separate AGPL-3.0 service; its upstream
[container setup](https://docs.searxng.org/admin/installation-docker.html) and
[license](https://github.com/searxng/searxng/blob/master/LICENSE) remain applicable.
Verify returned search results through `PublicWebClient` before marking it ready.

For the installed private service, keep the digest-pinned Compose definition at
`data/config/public-search/compose.json`, with operator-owned private `config`
and `cache` directories alongside it. The updater accepts only the isolated
search service: operator UID/GID, no published port, read-only configuration,
restricted container privileges and the installed app's default network. It
recreates that service after app-network creation and stops it before the app
network is removed. Explicitly disabled public web access does not start search.
Startup and doctor verify a bounded JSON search response from inside the app;
check relevant results and page retrieval separately because upstream engines
can fail or return poor results.

On a fresh installation, leave the internal search endpoint unset until this
private companion configuration has been provisioned, then set it and restart.
Missing or invalid configured search stops startup before containers are started.
When rolling back to a release older than 0.20.2, retain the newer verified
operator command for subsequent stop/start operations; older launchers do not
manage the separate search service.
