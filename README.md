# ansys-agent-bridge

An MCP server that drives **Ansys SpaceClaim** headlessly, plus a **DSH plugin
bundle** that installs it into a DeepSeek Harness profile in one command.

The point of this project is not to wrap every API. It is to report what
SpaceClaim actually did, in a place where the honest answer matters: two
operations on SpaceClaim 2024 R2 **return success and change nothing**, and an
agent that believes the return value will confidently describe geometry that
was never modified. This server measures the geometry before and after, and
raises `no_geometry_change` instead of reporting a success it cannot see.

## What it does

Eleven MCP tools:

| Tool | Read-only | What it does |
|---|---|---|
| `ansys_bridge_doctor` | yes | Reports detected Ansys releases, `SpaceClaim.exe`, Fluent root, Python, and package versions. Imports nothing heavyweight. |
| `scdm_session_start` | no | Starts a hidden SpaceClaim modeler (auto-detects the release; measured **30.6 s**). |
| `scdm_session_status` | yes | Whether a session is live, and its backend type/version. |
| `scdm_session_close` | no | Releases the session and its licence. |
| `scdm_open_file` | no | Opens `.scdoc`/`.scdocx`/`.dsco`/`.pmdb` and reports bodies, named selections, face counts, volumes. |
| `scdm_list_bodies` | yes | Body names, face counts, volumes for the open design. |
| `scdm_collisions` | yes | Pairwise collision state (`TOUCH`/`NONE`/…), all pairs or a chosen list. |
| `scdm_boolean` | no | `unite`/`subtract`/`intersect`, guarded by a before/after geometry check. |
| `scdm_share_topology` | no | Share topology, guarded by the same check. |
| `scdm_run_script` | no | Runs a headless IronPython script against the live session. |
| `scdm_export` | no | Exports `.scdocx`, STEP, IGES, Parasolid text/binary, `.pmdb`. |

### The two silent failures, measured

On a stator + 27 windings + pipe + inlet assembly (31 bodies, 846 faces):

**`subtract` fails, and the failure is easy to miss.** Target stator: 382 faces
and volume `0.003314505208 m³` before; **382 faces and the same volume after**.
The only thing that moved is the body count, 31 → 30, because `keep_other=False`
deleted the tool.

That matters for how you guard it. The obvious check — "did anything change?" —
answers *yes* here, so a guard built on it certifies the no-op. This server
therefore judges a boolean by **its target**: if the named target keeps the same
face count and volume, the call failed, whatever the body count says. The same
symptom appears through raw IronPython `Shape.Subtract`, so it is a
kernel/geometry problem with that model, not an API-path problem.

**`share_topology` returns `True` and does nothing.** Stator stays at 382 faces,
winding 1 at 16, bodies at 31.

**`unite` does work.** Stator faces `382 → 390`, volume
`0.003314505208 → 0.003382973513` (delta `6.84683e-5`, exactly winding 1's own
volume), total faces `846 → 838`, bodies `31 → 30`. Both number sets match the
raw IronPython result.

Full measured record, including the raw-IronPython traps (`Body[](n)` is a
parse-time error that kills a script silently; `Document.Load` breaks every
later `SaveAs`), is in [`skills/ansys-spaceclaim/SKILL.md`](skills/ansys-spaceclaim/SKILL.md).

## Install

Requires **Windows**, an Ansys installation with SpaceClaim (2024 R2 tested),
and [`uv`](https://docs.astral.sh/uv/).

### DSH (one command)

```sh
dsh plugin --profile web add ansys-agent-bridge
```

That installs the package, which declares `dsh.bundle`, so the loader applies
its `cordis.patch.yml`: one MCP client layer registering the `ansys` server.
Restart the profile and call `ansys_bridge_doctor`.

Turning it off again is the same in reverse:

```sh
dsh plugin --profile web remove ansys-agent-bridge
```

The MCP row launches the server with `uv tool run --from <this repo>`, so no
clone and no virtualenv is needed on the target machine — but the **first**
call pays for resolving and building `ansys-geometry-core`. If you keep a local
checkout and want a warm environment instead, point a `--patch` overlay at the
same server name with
`args: [run, --directory, <clone>/python, ansys-bridge-mcp]`.

The patch contains **no `!!js` and no absolute path**, which is deliberate: the
harness CLI (0.1.1-rc.1) evaluates `!!js` in a scope without `createRequire` and
does not await the result, so an expression using either boots on the Desktop app
(0.1.5-rc.2) and leaves the CLI with a profile that will not start. Both were
tried and both broke it; `command: uv` needs neither. Override that one line if
`uv` is not on the PATH the harness was launched with:

```yaml
# a --patch overlay, applied after the bundle layer
- id: mcp-ansys
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: ansys
    transport: stdio
    command: C:/Users/you/.local/bin/uv.exe
    args: [tool, run, --quiet, --from, <repo>, ansys-bridge-mcp]
    toolCallTimeoutMs: 900000
```

### The bundled skill

The same `!!js` restriction is why `skills/ansys-spaceclaim/` is **not** wired up
by the patch. Registering it needs a path resolved at load time, which needs one
of the two constructs above. Add it explicitly instead, in your profile's own
`cordis.patch.yml` (next to the bundle's) — this runs inside the host, where
`dshHomePath` is always available and no module resolution is involved:

```sh
mkdir -p "$DSH_HOME/skills"                      # or %APPDATA%\dsh-desktop\harness\skills
cp -r <repo>/skills/ansys-spaceclaim "$DSH_HOME/skills/"
```

`$DSH_HOME/skills` is one of the host's default skill roots, so nothing else is
needed. Without it the MCP tools still work; what you lose is the measured
operating notes and the trap list.

### Any other MCP client

The server is a plain stdio MCP server, so it is not tied to DSH. Generate the
block for your client:

```sh
uvx --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python" \
    ansys-bridge-doctor --config claude    # also: cursor, vscode, dsh
```

`claude` and `cursor` emit an `mcpServers` block, `vscode` emits a `servers`
block with an explicit `"type": "stdio"`, and `dsh` emits the `insert` patch
entry for a profile's `cordis.patch.yml`. Paste the result into the client's
configuration.

For a client whose schema you would rather write by hand, the command is:

```jsonc
{
  "mcpServers": {
    "ansys": {
      "command": "uv",
      "args": [
        "tool", "run", "--quiet",
        "--from", "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python",
        "ansys-bridge-mcp"
      ]
    }
  }
}
```

Use `uv tool run`, not `uv run`: measured against uv 0.11.29, `uv run` rejects
`--from` with `unexpected argument '--from' found`, and the MCP client shows
only `Connection closed`.

## Check the environment first

```sh
uvx --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python" \
    ansys-bridge-doctor
```

```
ansys-agent-bridge 0.1.0
  python        3.13.4  C:\...\python.exe
  platform      Windows-11-10.0.26100-SP0

ANSYS releases detected (AWP_ROOT* and standard install roots):
  242          C:\Program Files\ANSYS Inc\v242
                 SpaceClaim.exe: C:\Program Files\ANSYS Inc\v242\scdm\SpaceClaim.exe

  Fluent root   C:\Program Files\ANSYS Inc\v242\fluent

Python packages:
  ansys.geometry.core      0.17.2
  ansys.fluent.core        0.42.1
  mcp                      1.28.1

Ready:
  server       yes
  spaceclaim   yes
  fluent       yes
```

Add `--json` for the machine-readable report.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ANSYS_BRIDGE_UV` | — | Not read by the patch (which has no `!!js`). Override the `command:` line with a `--patch` overlay if `uv` is not on PATH. |
| `ANSYS_BRIDGE_PRELOAD` | `1` | Import the Ansys clients at start-up. Set `0` for a fast start; the first SpaceClaim call then pays that import. |
| `ANSYS_BRIDGE_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http`. |
| `ANSYS_BRIDGE_LOG_LEVEL` | `WARNING` | FastMCP request logging. `INFO` logs one line per request. |

### Why start-up preloads, and why tools are serialised

FastMCP runs a synchronous tool **on the event loop thread**
(`mcp/server/fastmcp/utilities/func_metadata.py`: `return fn(**args)` — there is
no `to_thread`). Lazy-importing `ansys.geometry.core` from inside a tool call
was measured wedging the whole process inside numpy's C extension
`create_module`, **with no exception and no timeout** — the client sees a tool
call that simply never returns:

```
_call_with_frames_removed (<frozen importlib._bootstrap>:488)
create_module (<frozen importlib._bootstrap_external>:1321)
<module> (numpy\_core\multiarray.py:11)
...
start (ansys_bridge_mcp\scdm.py)
scdm_session_start (ansys_bridge_mcp\server.py)
_handle_message (mcp\server\lowlevel\server.py)
```

The identical import finishes in **0.9 s** at process start, so `main()`
imports numpy and both Ansys clients before accepting a request. Stacks were
captured with `py-spy dump`.

The same fact means **long tools block the server**: a 30-second SpaceClaim
start holds the event loop, so a concurrent call queues behind it. That is
acceptable here — SpaceClaim mutates one design, and serialising is what you
want — but it is why `toolCallTimeoutMs` in the DSH patch is 900 s rather than
the 60 s default.

## Safety

- **Never open a file a SpaceClaim GUI has open, and never write to a source
  model.** Open, operate, export to a new path.
- A session holds a licence; call `scdm_session_close`.
- Package inspection is metadata-only. An earlier version imported
  `ansys.fluent.core` inside a tool call to read its version; that library
  prints during import, which corrupted the stdio JSON-RPC stream and killed
  the session mid-call. `package_version()` now reads metadata without
  importing.

## Layout

```
package.json            DSH bundle manifest (`dsh.bundle.patch`) + npm entry
cordis.patch.yml        the bundle's patch layers
skills/ansys-spaceclaim/SKILL.md   measured operating notes and traps
docs/feature-coverage.md           implementation vs. official manual, item by item
docs/verification.md               what was verified, and the bugs the run found
tools/verify-patch.mjs             offline check of cordis.patch.yml
python/                 the MCP server (uv/pip-installable, src layout)
```

## What is and is not implemented

[`docs/feature-coverage.md`](docs/feature-coverage.md) is the item-by-item
comparison against the official API: every capability domain, whether it is
implemented, and — for the gaps — why. It is not a list of what the server does;
it is the list a reader needs to find what it does not do.

The headline is that **the official client declares far more than this release
can run**. Of 286 public methods carrying a `@min_backend_version` gate, only
**15 are callable on 24R2**; the other 271 need 25.1 through 27.1. That includes
all 44 `GeometryCommands` modelling methods. The comparison therefore filters by
version first, which is what separates a real gap from a method that would only
raise `GeometryRuntimeError`.

Two consequences worth knowing before you plan work:

- **The highest-value gap is geometric inspection.** Eight `RepairTools.find_*`
  methods carry no version gate at all, so they work on 24R2 and are not
  implemented yet. They are also the official way to diagnose the
  `Found overlapping faces` failure that blocks Fluent Meshing.
- **An external flow enclosure cannot be built through this API.** The three
  `create_*_enclosure` methods need 26.1.0. For external-flow CFD, build the
  domain in the SpaceClaim UI or use Fluent Meshing's enclosure instead.


## Development

```sh
node tools/verify-patch.mjs        # no profile needed
uv run --directory python pytest -q
```

`verify-patch.mjs` evaluates any `!!js` expression in the patch inside a bare
`with (ctx) { eval(expr) }` scope — exactly what the CLI builds — and refuses a
promise result, so a patch that would only boot on the newer harness fails here
instead of on a user's machine. It also checks that the console script the patch
launches is the one `pyproject.toml` declares.

## Licence

MIT. See [LICENSE](LICENSE).
