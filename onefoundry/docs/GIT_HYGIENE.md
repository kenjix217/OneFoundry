# Git safety for the local operator checkout

Keep OneFoundry's live `.env`, PostgreSQL data, artifacts, output, local repository mounts, and backups outside Git. The ignore files prevent ordinary adds; `git_hygiene.py` also rejects a force-added runtime path or embedded Git repository. The check keeps documented environment samples, migrations, source, and synthetic tests available for review. New non-example email addresses are blocked in added lines; public GitHub no-reply addresses are permitted. Effective Git author and committer identities are checked too, including environment overrides. Use the repository owner's verified GitHub no-reply identity for real commits; reserved addresses are only for synthetic tests. History checks retain old identity findings even after local Git configuration changes.

Install the pinned Linux x64 scanner into this checkout's private Git directory, then enable the repository hooks:

```sh
python3 onefoundry/scripts/install_gitleaks.py .git/tools/gitleaks
git config --local core.hooksPath .githooks
```

The installer verifies the official Gitleaks v8.30.1 archive against its pinned SHA-256. The pre-commit hook checks the entire index, added email addresses, and staged secrets. The pre-push hook and CI check reachable history. A missing or wrong scanner version blocks both hooks. CI fetches full history and checks it even if the current tree is clean.

The email check distinguishes WeChat protocol account IDs ending in `@im.wechat` and URL userinfo from email addresses. Addresses in URL paths, queries, `mailto:` links, malformed authorities, and Git author/committer fields remain checked. The secret scanner still examines all files and credential-bearing URLs; no test-directory exclusion applies.

GitHub push protection is an additional check. It identified a repeated-`X` Stripe-shaped negative-test fixture during publication preparation. That test now constructs the same synthetic provider-shaped values at runtime and still proves rejection before storage. No push-protection exception or scanner exclusion was added.

Current source and intended untracked files had zero secret findings in a full candidate snapshot scan on 2026-10-15. The original historical email scan counted 136 matches, including synthetic protocol fields. After correcting that classification on 2026-10-16, the private development history still contains 11 excluded paths, 6 non-example email additions, 536 disallowed Git identity fields, and 48 Gitleaks candidate findings. Counts are not counts of confirmed personal mailboxes or confirmed live secrets. The old paths include 10 output files and one frozen legacy archive path. These findings require careful review and an approved history remediation before any push. No history was rewritten and no broad test-directory scanner exclusion was added.

Run the local checks without exposing values:

```sh
python3 onefoundry/scripts/git_hygiene.py
python3 onefoundry/scripts/git_hygiene.py --history
.git/tools/gitleaks git --staged --no-banner --no-color --redact --log-level=error .
.git/tools/gitleaks git --no-banner --no-color --redact --log-level=error --log-opts='--all' .
```

For a current full snapshot, copy only `git ls-files --cached --others --exclude-standard` regular files into a private temporary directory and run `gitleaks dir --redact` there. Do not scan the checkout directory directly, because it contains ignored live configuration and data.
