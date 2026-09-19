# ansys-agent-bridge (Python package)

The MCP server half of [ansys-agent-bridge](https://github.com/1622352030/ansys-agent-bridge).
It installs and answers `ansys-bridge-doctor` on a machine that has never seen
an Ansys installer; only the SpaceClaim tools need the runtime.

```sh
uv tool run --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python" \
    ansys-bridge-doctor
```

Run the server itself over stdio (this is what an MCP client does for you):

```sh
uv tool run --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python" \
    ansys-bridge-mcp
```

Use `uv tool run`, not `uv run` — `uv run` rejects `--from`. See the
[repository README](https://github.com/1622352030/ansys-agent-bridge#readme)
for the tool list, the two measured silent-failure modes, and the per-client
configuration blocks.

## Layout

| File | Purpose |
|---|---|
| `src/ansys_bridge_mcp/server.py` | FastMCP server and the fifteen tools. |
| `src/ansys_bridge_mcp/scdm.py` | SpaceClaim session and operators. |
| `src/ansys_bridge_mcp/guards.py` | Before/after geometry comparison, Ansys root detection, metadata-only version probes. |
| `src/ansys_bridge_mcp/doctor.py` | `ansys-bridge-doctor` and the per-client config emitters. |

## Licence

MIT.
