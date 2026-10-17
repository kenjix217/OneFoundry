"""Reject local runtime files and personal addresses before Git publication."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:\.env(?:$|\.(?!(?:[^/]*\.)?(?:example|sample|template)(?:/|$))[^/]*)|"
    r"compose\.local-source\.(?:json|ya?ml)|"
    r"\.infra-status(?:/|$)|\.offline-github-stage(?:/|$))"
    r"|^(?:onefoundry/(?:output|data|backups?|pgdata|repositories|checkouts|"
    r"worktrees|volumes)(?:/|$)|output/playwright(?:/|$)|legacy/kj31(?:/|$))"
    r"|\.(?:db|sqlite3?|dump|backup|pgdump|pem|p12|pfx|key|kdbx|zip|tgz)$"
    r"|\.tar(?:\.gz)?$",
    re.IGNORECASE,
)
EMAIL = re.compile(
    r"(?<![A-Za-z0-9_.+-])[A-Za-z0-9_.+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
    r"(?![A-Za-z0-9_.+-])"
)
WEIXIN_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+@im\.wechat", re.IGNORECASE)
URL_AUTHORITY = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/\"'<>?#]+")
RESERVED_DOMAINS = {"example.com", "example.org", "example.net"}
RESERVED_SUFFIXES = (".example", ".invalid", ".test", ".localhost", ".local")
IDENTITY_SUFFIXES = (".example", ".invalid", ".test", ".localhost")
IDENT = re.compile(rb"[^\r\n<>]* <([^<>\r\n]+)> [0-9]+ [+-][0-9]{4}\n?")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> bytes:
    # Git receives fixed internal argument lists; no shell or user command text is used.
    git_executable = shutil.which("git")
    if git_executable is None:
        raise FileNotFoundError("Git executable is required")
    return subprocess.check_output(  # noqa: S603
        [git_executable, *args], cwd=REPO_ROOT, stderr=subprocess.PIPE
    )


def inventory_violations(index: bytes) -> tuple[int, int]:
    """Return private-path and gitlink counts without disclosing path names."""
    private = gitlinks = 0
    for entry in index.split(b"\0"):
        if not entry:
            continue
        metadata, path_bytes = entry.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        path = path_bytes.decode("utf-8", errors="surrogateescape")
        private += bool(PRIVATE_PATH.search(path))
        gitlinks += mode == b"160000"
    return private, gitlinks


def historical_path_violations(paths: bytes) -> int:
    """Count private paths that appeared anywhere in reachable Git history."""
    unique = set()
    for entry in paths.split(b"\0"):
        path = entry.strip(b"\n").decode("utf-8", errors="surrogateescape")
        if path and PRIVATE_PATH.search(path):
            unique.add(path)
    return len(unique)


def email_violations(patch: str) -> int:
    """Count new non-example addresses in a Git patch."""
    count = 0
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        authorities = [
            match.span() for match in URL_AUTHORITY.finditer(added) if match.group().count("@") == 1
        ]
        for match in EMAIL.finditer(added):
            # WeChat account IDs and URI userinfo are protocol fields, not mail
            # addresses. Secret scanning independently checks credential values.
            if WEIXIN_ACCOUNT_ID.fullmatch(match.group()) or any(
                start <= match.start() and match.end() <= end for start, end in authorities
            ):
                continue
            domain = match.group().rsplit("@", 1)[1].lower()
            if not allowed_git_identity(match.group()) and not domain.endswith(RESERVED_SUFFIXES):
                count += 1
    return count


def allowed_git_identity(email: str) -> bool:
    """Accept only public no-reply or documentation-only commit addresses."""
    if EMAIL.fullmatch(email) is None:
        return False
    normalized = email.lower()
    if normalized == "noreply@github.com":
        return True
    domain = normalized.rsplit("@", 1)[1]
    return (
        domain == "users.noreply.github.com"
        or domain in RESERVED_DOMAINS
        or domain.endswith(IDENTITY_SUFFIXES)
    )


def effective_identity_violations(author_ident: bytes, committer_ident: bytes) -> int:
    """Check Git's resolved identities, including environment overrides."""
    count = 0
    for ident in (author_ident, committer_ident):
        match = IDENT.fullmatch(ident)
        if match is None or not allowed_git_identity(match.group(1).decode("utf-8", errors="replace")):
            count += 1
    return count


def historical_identity_violations(metadata: bytes) -> int:
    """Check both address fields of every reachable commit."""
    count = 0
    for record in metadata.split(b"\0"):
        record = record.strip(b"\n")
        if not record:
            continue
        fields = record.split(b"\t")
        if len(fields) != 2:
            count += 2
            continue
        count += sum(not allowed_git_identity(field.decode("utf-8", errors="replace")) for field in fields)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--range", nargs=2, metavar=("BASE", "HEAD"))
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    try:
        private, gitlinks = inventory_violations(_git("ls-files", "--stage", "-z"))
        historical = (
            historical_path_violations(
                _git("log", "--all", "--name-only", "--format=", "--diff-filter=A", "-z")
            )
            if args.history
            else 0
        )
        historic_email_patch = (
            _git(
                "log",
                "--all",
                "-p",
                "--format=",
                "--unified=0",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
            )
            if args.history
            else b""
        )
        identity_violations = (
            historical_identity_violations(_git("log", "--all", "--format=%ae%x09%ce%x00"))
            if args.history
            else effective_identity_violations(
                _git("var", "GIT_AUTHOR_IDENT"), _git("var", "GIT_COMMITTER_IDENT")
            )
        )
        if args.range:
            patch = _git(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--unified=0",
                "--no-renames",
                args.range[0],
                args.range[1],
                "--",
            )
        else:
            patch = _git(
                "diff", "--cached", "--no-ext-diff", "--no-textconv", "--unified=0", "--no-renames", "--"
            )
    except (OSError, subprocess.CalledProcessError, ValueError):
        print("Git hygiene check failed to inspect the index or patch", file=sys.stderr)
        return 2
    emails = email_violations(patch.decode("utf-8", errors="replace"))
    historical_emails = email_violations(historic_email_patch.decode("utf-8", errors="replace"))
    if private or gitlinks or emails or historical or historical_emails or identity_violations:
        print(
            f"Git hygiene blocked: {private} private runtime paths, "
            f"{gitlinks} gitlinks, {emails} new non-example email addresses, "
            f"{historical} historical private paths, "
            f"{historical_emails} historical non-example email additions, "
            f"{identity_violations} disallowed Git identity fields. "
            "Inspect locally; no values or paths are printed.",
            file=sys.stderr,
        )
        return 1
    print("Git hygiene: path, email, and Git identity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
