"""Create one controlled, local bare Git remote for offline browser acceptance."""

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git with only local fixture paths
        ["/usr/bin/git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    part = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
    if not part.fullmatch(args.owner) or not part.fullmatch(args.repo):
        parser.error("owner and repo must be simple GitHub name parts")
    if not args.output.is_absolute() or args.output.exists() or args.output.is_symlink():
        parser.error("output must be a new absolute path")
    if (
        args.output.parent.is_symlink()
        or not args.output.parent.is_dir()
        or args.output.parent.resolve() != args.output.parent
    ):
        parser.error("output parent must be an existing directory")
    with tempfile.TemporaryDirectory(prefix="onefoundry-synthetic-gh-") as temp:
        work = Path(temp) / "work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        git(work, "config", "user.name", "Fixture")
        git(work, "config", "user.email", "fixture@example.invalid")
        (work / "file.txt").write_text("synthetic offline stage\n")
        git(work, "add", "file.txt")
        git(work, "commit", "-qm", "synthetic fixture")
        subprocess.run(  # noqa: S603 - new bare repository at the explicit local output
            ["/usr/bin/git", "init", "--bare", "-q", str(args.output)],
            check=True,
        )
        git(work, "push", "-q", str(args.output), "main:main")
        revision = git(work, "rev-parse", "HEAD")
        tree = git(work, "rev-parse", "HEAD^{tree}")
        (args.output / "onefoundry-synthetic-fixture.json").write_text(
            json.dumps(
                {
                    "kind": "onefoundry_synthetic_github_fixture_v1",
                    "owner": args.owner,
                    "repo": args.repo,
                    "revision": revision,
                },
                sort_keys=True,
            )
        )
    print(json.dumps({"revision": revision, "tree": tree, "remote": str(args.output)}))


if __name__ == "__main__":
    main()
