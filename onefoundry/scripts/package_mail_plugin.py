"""Package private plugin source, or a connection after the operator verified its endpoint."""

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from onefoundry.mail_plugin_settings import MailPluginSettings

PACKAGE_NAME = "zoho-mail-read"
SOURCE = Path(__file__).resolve().parents[1] / "plugins" / PACKAGE_NAME
FILES = ("plugin.json", "README.md", "skills/zoho-mail-read/SKILL.md")


def package(output: Path, *, verified_mcp_url: str | None = None) -> None:
    if verified_mcp_url is not None:
        MailPluginSettings.mcp_path(MailPluginSettings.https_only(verified_mcp_url))
    if output.exists() or SOURCE in output.resolve().parents:
        raise ValueError("use a new archive path outside the plugin source directory")
    manifest = json.loads((SOURCE / "plugin.json").read_text())
    interface = manifest["extensions"]["com.openai"]["interface"]
    if manifest["name"] != PACKAGE_NAME or len(interface["shortDescription"]) > 30:
        raise ValueError("invalid private plugin manifest")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name in FILES:
            path = SOURCE / name
            if path.resolve() != path or not path.is_file():
                raise ValueError("package source must contain only regular files")
            archive.writestr(f"{PACKAGE_NAME}/{name}", path.read_bytes())
        if verified_mcp_url is not None:
            config = {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {"zoho-mail-read": {"type": "streamable-http", "url": verified_mcp_url}},
            }
            archive.writestr(f"{PACKAGE_NAME}/mcp.json", json.dumps(config, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--verified-mcp-url", help="approved HTTPS /mcp URL already verified with OAuth and tools/list"
    )
    args = parser.parse_args()
    package(args.output, verified_mcp_url=args.verified_mcp_url)
    print(f"Private {'connection' if args.verified_mcp_url else 'source'} archive: {args.output}")


if __name__ == "__main__":
    main()
