"""`ansys-bridge-doctor` — diagnose the host and emit client configuration.

Two jobs, one command:

* say what this machine can actually do (releases, executables, packages),
* print the config block that other MCP clients need, so the same server can be
  installed anywhere that speaks MCP without hand-writing a path.

    ansys-bridge-doctor
    ansys-bridge-doctor --config claude
    ansys-bridge-doctor --config cursor
    ansys-bridge-doctor --config vscode
    ansys-bridge-doctor --config generic
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from typing import Any

from . import __version__
from .guards import detect_ansys_roots, detect_fluent_root, package_version

# The command that starts the server. `uv run --from` keeps it portable: it
# resolves the package from git or PyPI on first use and caches it, so no
# absolute path to a checkout or a virtualenv appears in anyone's config.
GIT_URL = "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python"
UV_FROM = ["uv", "run", "--from", GIT_URL, "ansys-bridge-mcp"]


def _packages() -> dict[str, str]:
    # Metadata only: importing ansys.fluent.core prints to stdout, which would
    # corrupt the JSON-RPC stream when this runs inside a tool call.
    out: dict[str, str] = {}
    for module in ("ansys.geometry.core", "ansys.fluent.core", "mcp"):
        out[module] = package_version(module) or "missing"
    return out


def _uv_start() -> list[str]:
    """Prefer a plain console script when the package is already installed."""
    exe = shutil.which("ansys-bridge-mcp")
    if exe:
        return [exe]
    if shutil.which("uv"):
        return UV_FROM
    return [sys.executable, "-m", "ansys_bridge_mcp.server"]


def collect() -> dict[str, Any]:
    ansys = detect_ansys_roots()
    packages = _packages()
    return {
        "bridge_version": __version__,
        "python": {
            "executable": sys.executable,
            "version": ".".join(str(p) for p in sys.version_info[:3]),
            "platform": platform.platform(),
        },
        "ansys": ansys,
        "fluent_root": detect_fluent_root(),
        "packages": packages,
        "start_command": _uv_start(),
        "ready": {
            # The MCP server itself needs nothing but `mcp`.
            "server": not str(packages.get("mcp", "")).startswith("missing"),
            # SpaceClaim tools additionally need the geometry client and a release.
            "spaceclaim": bool(ansys["spaceclaim"])
            and not str(packages.get("ansys.geometry.core", "")).startswith("missing"),
            "fluent": bool(detect_fluent_root())
            and not str(packages.get("ansys.fluent.core", "")).startswith("missing"),
        },
    }


_CONFIGS = {
    "claude": lambda cmd: {
        "mcpServers": {"ansys": {"command": cmd[0], "args": cmd[1:]}}
    },
    "cursor": lambda cmd: {
        "mcpServers": {"ansys": {"command": cmd[0], "args": cmd[1:]}}
    },
    "vscode": lambda cmd: {
        "servers": {"ansys": {"type": "stdio", "command": cmd[0], "args": cmd[1:]}}
    },
    "dsh": lambda cmd: {
        "insert": [
            {
                "id": "mcp-ansys",
                "name": "@deepseek-ai/dsh-mcp-client",
                "config": {
                    "serverName": "ansys",
                    "transport": "stdio",
                    "command": cmd[0],
                    "args": cmd[1:],
                },
            }
        ]
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="ansys-bridge-doctor", description=__doc__)
    parser.add_argument(
        "--config",
        choices=sorted(_CONFIGS),
        help="print the client configuration block for this platform instead of the report",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()

    report = collect()

    if args.config:
        config = _CONFIGS[args.config](report["start_command"])
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    print(f"ansys-agent-bridge {report['bridge_version']}")
    print(f"  python        {report['python']['version']}  {report['python']['executable']}")
    print(f"  platform      {report['python']['platform']}")
    print()
    print("ANSYS releases detected (AWP_ROOT* and standard install roots):")
    releases = report["ansys"]["releases"]
    if not releases:
        print("  none - SpaceClaim tools will not work on this machine")
    for version, root in releases.items():
        label = version or "(unversioned)"
        sc = report["ansys"]["spaceclaim"].get(version)
        print(f"  {label:<12} {root}")
        print(f"  {'':<12}   SpaceClaim.exe: {sc or 'not found'}")
    print()
    print(f"  Fluent root   {report['fluent_root'] or 'not found'}")
    print()
    print("Python packages:")
    for name, version in report["packages"].items():
        print(f"  {name:<24} {version}")
    print()
    ready = report["ready"]
    print("Ready:")
    for name in ("server", "spaceclaim", "fluent"):
        print(f"  {name:<12} {'yes' if ready[name] else 'no'}")
    print()
    print("Start command:")
    print("  " + " ".join(report["start_command"]))
    print()
    print("Client configuration (ansys-bridge-doctor --config "
          "{claude|cursor|vscode|dsh})")


if __name__ == "__main__":
    main()
