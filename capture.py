"""Capture a manifest from running backends; fail without overwriting on errors."""
import argparse
import asyncio
import json
from pathlib import Path

from fastmcp import Client


async def capture(config: dict) -> dict:
    servers = config["mcpServers"]
    if not isinstance(servers, dict) or not servers:
        raise ValueError("mcpServers must contain at least one backend")
    manifest = {}
    for name, backend in servers.items():
        async with Client(backend["url"]) as client:
            tools = await client.list_tools()
        manifest[name] = {
            "url": backend["url"],
            "container": backend["container"],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                    "output_schema": tool.outputSchema,
                }
                for tool in tools
            ],
        }
        print(f"Captured {len(tools)} tools for {name}", flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("backends.json"))
    parser.add_argument("--output", type=Path, default=Path("manifest.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    manifest = asyncio.run(capture(config))
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(args.output)
    print(f"Wrote {len(manifest)} backends to {args.output}", flush=True)


if __name__ == "__main__":
    main()
