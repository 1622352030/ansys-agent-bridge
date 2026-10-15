# ansys-agent-bridge (Python package)

The MCP server half of [ansys-agent-bridge](https://github.com/1622352030/ansys-agent-bridge).
It installs and answers `ansys-bridge-doctor` on a machine that has never seen
an Ansys installer; only the SpaceClaim tools need the runtime.

```sh
uv tool run --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=spaceclaim/python" \
    ansys-bridge-doctor
```

Run the server itself over stdio (this is what an MCP client does for you):

```sh
uv tool run --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=spaceclaim/python" \
    ansys-bridge-mcp
```

Use `uv tool run`, not `uv run` — `uv run` rejects `--from`. See the
[repository README](https://github.com/1622352030/ansys-agent-bridge#readme)
for the tool list, the two measured silent-failure modes, and the per-client
configuration blocks.

## Layout

| File | Purpose |
|---|---|
| `src/ansys_bridge_mcp/server.py` | FastMCP server and the nineteen tools. |
| `src/ansys_bridge_mcp/scdm.py` | SpaceClaim session, operators, and the creation tools (which drive geometry through generated IronPython). |
| `src/ansys_bridge_mcp/guards.py` | Before/after geometry comparison, Ansys root detection, metadata-only version probes. |
| `src/ansys_bridge_mcp/doctor.py` | `ansys-bridge-doctor` and the per-client config emitters. |

## Creating geometry

`scdm_create_pipe` and `scdm_extrude` are the two tools that **add** geometry; every
other `scdm_*` tool reads the model or operates on bodies that already exist.

They work by generating IronPython and running it through the same service channel
`scdm_run_script` uses, because the geometry calls they need do not exist in a
usable form on this release:

- `GeometryCommands.extrude_faces` is version-gated to **25R2** and raises on 24R2
  before reaching the service.
- `Plane.Create(...)`, `Matrix.CreateTranslation(...)` and assigning
  `Matrix.Translation` all raise a bare `TypeError` from the interop binder, so a
  section is built directly on one of the three **static** planes
  (`Plane.PlaneXY` / `PlaneYZ` / `PlaneZX`) at its final position, with no transform.
- `ExtrudeFaces.Execute` reports **zero** `CreatedBodies` even when the extrusion
  succeeded, so what was created is established by diffing the document.

Every generated script verifies its own result and reports failures through a
`result['failed']` tag, because the service collapses every IronPython exception
into the same two-word `"Script run failed."` — a tag is the only thing that reaches
the caller.

The measured API facts, the traps, and the two things that remain unsolved
(`Profile` cannot be constructed; `DesignFace` cannot be typed) are recorded in
[`../docs/verification.md`](../docs/verification.md).

## Licence

MIT.
