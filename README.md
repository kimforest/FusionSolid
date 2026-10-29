# fusion2solid

Migrate a **parametric design history from Autodesk Fusion 360 into SolidCraft**.

Open the design in Fusion, start SolidCraft, run one command, and the tool reads every
item in the Fusion timeline and replays it as a SolidCraft feature — sketches, extrudes,
revolves, fillets, combines, patterns, parameters and the rest — then saves the result and
writes a report of exactly what was migrated, skipped or failed.

```
python -m fusion2solid migrate
```

```
Fusion design 'Trouble_Pieces v2': 0 parameters, 2 steps
SolidCraft : control channel 127.0.0.1:27878
Replayed 2 steps (2 ok, 0 failed)
Saved    : C:\...\reports\Trouble_Pieces v2-migrated.sc
Summary  : 2 mapped, 0 skipped, 0 failed (of 2 steps)
Report   : reports\report.md
```

---

## How it works

Both applications are driven over MCP (Model Context Protocol), so nothing is exported and
re-imported: the **feature history** is transferred, not a dumb solid.

| Side | How it is reached |
| --- | --- |
| Fusion 360 | Autodesk's built-in **Fusion Desktop MCP** server, Streamable HTTP at `http://127.0.0.1:27182/mcp` |
| SolidCraft | The running app's **control channel** (default `127.0.0.1:27878`), or `solidcraft-cli mcp` as a stdio MCP server |

The pipeline is:

1. **Discover** the Fusion MCP tool surface (`tools/list`). Fusion exposes a script runner, so
   the extractor sends a small read-only Python program that runs inside Fusion and returns the
   whole timeline as JSON.
2. **Extract** the design into an intermediate representation: user parameters, sketches
   (curves, points, constraints, dimensions, plane frame) and features (type, options, inputs),
   all normalized to millimetres and degrees.
3. **Map** that onto SolidCraft commands (`solid.extrude`, `solid.fillet`, `sketch.circle.center`,
   …). The mapping is checked against the live SolidCraft command catalog, so parameters the
   installed version does not accept are dropped and reported rather than sent blindly.
4. **Replay** the commands in order into a new design, resolving each feature's inputs to the
   geometry SolidCraft just built (a sketch's profile, a body's face or edge) by matching the
   geometry Fusion recorded.
5. **Verify** and write a report, including a volume comparison against the original.

---

## Requirements

* **Python 3.9+** — no third-party packages are needed.
* **Fusion 360** with the MCP API enabled:
  *Fusion → Preferences → General → API* (the server listens on `127.0.0.1:27182`).
  The design you want to migrate must be **open and active**.
* **SolidCraft** running, with its control channel enabled (on by default:
  *Preferences → Assistant → "Let AI agents connect"*).
  Optional: `solidcraft-cli` on `PATH` or at
  `C:\Program Files\SolidCraft\solidcraft-cli.exe`.
* Fusion and SolidCraft must run on the **same machine**.

---

## Getting started

Run from the project folder (the folder that contains `fusion2solid/`):

```powershell
# 0. Check that both sides answer and see what the design contains.
python -m fusion2solid discover

# 1. Extract and map, but change nothing in SolidCraft. Read the report.
python -m fusion2solid dry-run

# 2. Do it for real: build the design in SolidCraft and save it.
python -m fusion2solid migrate
```

`discover` is the safe first step: it prints the Fusion tools it found, a strategy, and a
summary of the active design (parameter count, timeline length, feature-type histogram). It
writes `discovery/fusion-tools.json` and `discovery/design-summary.json` so you can inspect
everything before committing to a migration.

`dry-run` connects to SolidCraft only to read its command catalog and never creates or edits a
design. Use it to see the full plan and every unmapped step up front.

> **`migrate` creates a new design** in the running SolidCraft app (it runs `file.new`). If the
> new design takes over the tab you were working in, close it / switch back afterwards.

---

## Commands

| Command | What it does | Changes SolidCraft? |
| --- | --- | --- |
| `discover` | Dump the Fusion MCP tools and a summary of the active design | no |
| `dry-run` | Extract, map and write the report | no (reads the catalog only) |
| `migrate` | Extract, map, replay, save, screenshot and write the report | **yes** |

Exit codes: `0` success · `1` one or more steps failed · `2` connection/configuration error ·
`3` (discover only) Fusion exposes no usable tool.

---

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--fusion-url` | `http://127.0.0.1:27182/mcp` | Fusion MCP endpoint |
| `--fusion-read-only` | off | Run the Fusion extractor read-only. Fusion only exposes some feature inputs (fillet edges, combine bodies) while the timeline marker sits *on* that feature, which needs a writable context; with this flag those features are reported as skipped instead. The design itself is never modified either way. |
| `--transport` | `auto` | How to reach SolidCraft: `cli` (spawn `solidcraft-cli mcp`), `control` (talk to the app's control channel directly), or `auto` (prefer the CLI when installed, otherwise the control channel) |
| `--solidcraft-port` | from the app | SolidCraft control port. By default the app's `control/current.port` is read, else `27878` |
| `--solidcraft-cli` | `C:\Program Files\SolidCraft\solidcraft-cli.exe` | Path to `solidcraft-cli` |
| `--allow-path` | — | Folder the SolidCraft side may write in. Repeatable. The report folder and `--out` folder are added automatically |
| `--out` | `<report-dir>\<design>-migrated.sc` | Where to save the migrated design |
| `--report-dir` | `reports` | Where `report.json`, `report.md`, the screenshot and the log go |
| `--discovery-dir` | `discovery` | Where `discover` writes its JSON |
| `--tolerance` | `1e-2` | Geometry-match tolerance in mm when resolving faces/edges |
| `--only` / `--skip` | — | Substring filters on feature names; repeatable |
| `--timeout` | `330` | Per-call timeout in seconds (SolidCraft can block up to 300 s on a slow rebuild) |
| `--no-save` | off | Replay but do not write a `.sc` file |
| `--no-screenshot` | off | Skip the validation screenshot |

Examples:

```powershell
# Rebuild only the outer shape first
python -m fusion2solid migrate --only Extrude --only Fillet

# Drive the app explicitly over the control channel, write somewhere specific
python -m fusion2solid migrate --transport control --out "C:\work\part.sc" --allow-path "C:\work"

# Extract read-only (fillet/combine inputs will be reported as skipped)
python -m fusion2solid dry-run --fusion-read-only
```

---

## Output

```
reports/
  report.json               machine-readable report
  report.md                 human-readable report
  migrated.png              iso screenshot of the result (unless --no-screenshot)
  solidcraft-stderr.log     SolidCraft side's stderr (CLI transport)
  <design>-migrated.sc      the migrated design
discovery/
  fusion-tools.json         the Fusion MCP tools/list dump (name + input schema)
  fusion-discovery.json     which tool was chosen and why
  design-raw.json           the raw extraction, exactly as Fusion returned it
  design-summary.json       counts and feature-type histogram
```

`report.md` / `report.json` contain:

* a summary (`mapped` / `skipped` / `failed` / `parameters`),
* a **validation** block: Fusion's total volume and bounding boxes versus SolidCraft's, plus the
  relative volume difference,
* one row per timeline step with its status and, when relevant, the SolidCraft error text and
  notes,
* the list of unmapped features with the reason, and
* the inputs Fusion refused to expose (`unreadable_inputs`), if any.

If the migrated volume differs from Fusion's by more than **1%**, `migrate` prints a warning and
the numbers are in the report — that is the signal that the geometry is **not** faithful and
needs manual review.

---

## What is covered, and what is not

**Covered:** the timeline in order — user parameters, sketches (lines, circles, arcs, ellipses,
points, dimensions, constraints) and the solid features SolidCraft knows: extrude, revolve,
fillet, chamfer, hole, shell, sweep, loft, pipe, coil, combine, split body, scale, draft, thread,
rib, web, emboss, remove, offset/replace face, boundary fill, move, circular/rectangular/path
patterns, mirror, primitives, and world construction axes.

**Not migrated (reported, never guessed):**

* construction planes, axes and points as geometry (sketches that used them are still placed, by
  their frame),
* assemblies, components and joints — this is a single-part history tool,
* surface features and plastic/sheet-metal specialty features,
* sketches constraints/dimensions that reference *points*: Fusion's point order does not line up
  with SolidCraft's generated points, so they are skipped (SolidCraft already joins points that
  land on existing ones),
* any feature whose inputs Fusion will not expose through its API (reported with the reason),
* a whole-model STEP fallback: there is none — this is a history replay.

**Known approximations:**

* a feature whose profile comes from a single-profile sketch is extruded with "all profiles of
  the most recent sketch"; a multi-profile sketch uses a point inside each profile, falling back
  to "all" when profiles are nested (nested profiles share a centre),
* ellipses are placed with their major axis assumed along the sketch x axis,
* arcs are emitted counter-clockwise from their start point (SolidCraft's convention),
* bodies are matched between the two applications by name, then by bounding-box centre.

Some Fusion features simply cannot be read: their properties raise
*"Didn't roll editing feature back"* unless the timeline marker is parked on them, and Fusion
hides a few (for example an `OffsetFaces` feature's faces) at every marker position. Those steps
appear as **skipped** with the reason, never as silent guesses.

---

## Troubleshooting

**`error: could not reach http://127.0.0.1:27182/mcp`**
Fusion is not running, the design is not open, or the MCP API is off. Check
*Preferences → General → API*.

**`error: cannot reach SolidCraft: ... no control token exists for port 27878`**
The SolidCraft app is not running (its control channel writes a token per launch at
`%LOCALAPPDATA%\SolidCraft\control\<port>.token`). Start it, or pass `--transport cli` with a
`solidcraft-cli` path.

**`... outside the folders this agent may use`**
Add the folder with `--allow-path "C:\abs\folder"`. Paths must be absolute and exist.

**`still running:` / a step times out**
SolidCraft is still rebuilding; the tool waits via its `pending` mechanism. Do not re-run
`migrate` against the same design — it always starts a fresh one, so a re-run is safe but
discards the previous attempt.

**Steps are `skipped` with "Fusion did not expose the input(s) ..."**
Fusion would not reveal that feature's inputs read-only. Re-run without `--fusion-read-only`
(the default) so the extractor can move the timeline marker and put it back.

**"migrated volume differs from Fusion by X%"**
The rebuilt geometry is not faithful. Read the report: usually a profile or a referenced edge
was matched approximately, or an upstream step failed. Compare `migrated.png` against the
Fusion model.

**See exactly what is sent to SolidCraft**
Set `F2S_DEBUG=1` to print each resolved batch of commands to stderr.

---

## Development

```powershell
# The offline test suite (no Fusion or SolidCraft needed).
python -m unittest discover -s tests -t .

# Byte-compile everything.
python -m compileall -q fusion2solid tests
```

Layout:

```
fusion2solid/
  cli.py              commands, options, transport selection
  discovery.py        Fusion tool classification, script-argument assembly
  fusion_script.py    the extractor program that runs inside Fusion
  fusion_source.py    Fusion MCP client, raw JSON -> intermediate representation
  ir.py               the intermediate representation (mm, degrees)
  mapper.py           IR -> SolidCraft commands (data-driven tables)
  refs.py             geometric face/edge/vertex matching
  solidcraft_sink.py  replay, reference resolution, result harvesting
  control_bridge.py   SolidCraft control-channel transport (no CLI required)
  mcp_client.py       stdio + Streamable HTTP JSON-RPC client
  report.py           report.json / report.md
tests/                offline tests, including a stub SolidCraft MCP server
```

The tests include an end-to-end replay against `tests/fake_solidcraft.py`, a stub MCP server, so
the sink, reference resolution and harvesting are exercised without either application running.
