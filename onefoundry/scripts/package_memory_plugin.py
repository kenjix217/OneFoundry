"""Export the private memory plugin source or a verified HTTPS MCP connection."""

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from onefoundry.memory_plugin_settings import https_url

PACKAGE_NAME = "onefoundry-company-memory"
SOURCE = Path(__file__).resolve().parents[1] / "plugins" / PACKAGE_NAME
FILES = ("plugin.json", "README.md", "skills/company-memory/SKILL.md", "scripts/memory_bridge.py")


def package(
    output: Path, *, verified_mcp_url: str | None = None, local_client_config: Path | None = None
) -> None:
    if local_client_config is not None and (
        verified_mcp_url is not None
        or not local_client_config.is_absolute()
        or ".." in local_client_config.parts
    ):
        raise ValueError("choose one connection and an absolute private client configuration path")
    if verified_mcp_url is not None:
        https_url(verified_mcp_url)
    if output.exists() or SOURCE in output.resolve().parents:
        raise ValueError("choose a new archive path outside the plugin source directory")
    manifest = json.loads((SOURCE / "plugin.json").read_text())
    if (
        manifest["name"] != PACKAGE_NAME
        or len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) > 30
    ):
        raise ValueError("invalid plugin identity or subtitle")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name in FILES:
            path = SOURCE / name
            if path.resolve() != path or not path.is_file():
                raise ValueError("plugin source must contain only regular files")
            archive.writestr(f"{PACKAGE_NAME}/{name}", path.read_bytes())
        if verified_mcp_url is not None:
            archive.writestr(
                f"{PACKAGE_NAME}/mcp.json",
                json.dumps(
                    {
                        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                        "mcpServers": {PACKAGE_NAME: {"type": "streamable-http", "url": verified_mcp_url}},
                    },
                    indent=2,
                )
                + "\n",
            )
        elif local_client_config is not None:
            archive.writestr(
                f"{PACKAGE_NAME}/mcp.json",
                json.dumps(
                    {
                        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                        "mcpServers": {
                            PACKAGE_NAME: {
                                "type": "stdio",
                                "command": "python3",
                                "args": [
                                    "${PLUGIN_ROOT}/scripts/memory_bridge.py",
                                    "--config",
                                    str(local_client_config),
                                ],
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--verified-mcp-url", help="approved HTTPS endpoint after authentication and tools/list verification"
    )
    parser.add_argument(
        "--local-client-config", type=Path, help="private key configuration path; contents are never packaged"
    )
    args = parser.parse_args()
    package(args.output, verified_mcp_url=args.verified_mcp_url, local_client_config=args.local_client_config)
    mode = "connection" if args.verified_mcp_url or args.local_client_config else "source-only"
    print(f"Private {mode} package: {args.output}")


if __name__ == "__main__":
    main()
