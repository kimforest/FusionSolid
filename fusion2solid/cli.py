"""Command line entry point.

    fusion2solid discover    # Task 0 only: dump the Fusion tools and a design summary
    fusion2solid dry-run     # extract + map + report, change nothing in SolidCraft
    fusion2solid migrate     # extract + map + replay + save + report
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import control_bridge, discovery as disc
from .fusion_source import FusionSource
from .mapper import Catalog, Plan, map_design
from .mcp_client import MCPError
from .report import build_report, write_report
from .solidcraft_sink import SolidCraftSink, StepOutcome

DEFAULT_FUSION_URL = "http://127.0.0.1:27182/mcp"
DEFAULT_SOLIDCRAFT_PORT = 27878
DEFAULT_CLI = r"C:\Program Files\SolidCraft\solidcraft-cli.exe"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fusion2solid",
        description="Migrate a Fusion 360 design history into SolidCraft over MCP.",
    )
    parser.add_argument("--fusion-url", default=DEFAULT_FUSION_URL,
                        help="Fusion MCP endpoint (default: %(default)s)")
    parser.add_argument("--fusion-read-only", action="store_true",
                        help="run the Fusion extractor read-only. Fusion only exposes some "
                             "feature inputs (fillet edges, combine bodies) while the timeline "
                             "marker sits on that feature, which needs a writable context; with "
                             "this flag those features are reported as skipped instead.")
    parser.add_argument("--solidcraft-port", type=int, default=None,
                        help="SolidCraft app control port (default: the app's current.port, else %d)"
                             % DEFAULT_SOLIDCRAFT_PORT)
    parser.add_argument("--transport", choices=("auto", "cli", "control"), default="auto",
                        help="how to reach SolidCraft: 'cli' spawns solidcraft-cli mcp (stdio MCP), "
                             "'control' speaks the app's control channel directly, 'auto' prefers "
                             "the CLI when it is installed (default: %(default)s)")
    parser.add_argument("--solidcraft-cli", default=DEFAULT_CLI,
                        help="solidcraft-cli executable (default: %(default)s)")
    parser.add_argument("--allow-path", action="append", default=[],
                        help="folder the SolidCraft agent may write in (repeatable)")
    parser.add_argument("--out", default=None,
                        help="where to save the migrated design (default: <design>-migrated.sc)")
    parser.add_argument("--report-dir", default="reports", help="report output folder")
    parser.add_argument("--tolerance", type=float, default=1e-2,
                        help="geometry match tolerance in mm (default: %(default)s)")
    parser.add_argument("--only", action="append", default=[],
                        help="migrate only features whose name matches (repeatable)")
    parser.add_argument("--skip", action="append", default=[],
                        help="do not migrate features whose name matches (repeatable)")
    parser.add_argument("--timeout", type=float, default=330.0, help="per-call timeout in seconds")
    parser.add_argument("--no-save", action="store_true", help="do not save the SolidCraft design")
    parser.add_argument("--no-screenshot", action="store_true", help="skip the validation screenshot")
    parser.add_argument("--discovery-dir", default="discovery",
                        help="where to write the tool dump (default: %(default)s)")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="dump the Fusion MCP tool surface and a design summary")
    sub.add_parser("dry-run", help="extract and map, but do not change SolidCraft")
    sub.add_parser("migrate", help="extract, map and replay into SolidCraft")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            return _cmd_discover(args)
        if args.command == "dry-run":
            return _cmd_run(args, dry_run=True)
        if args.command == "migrate":
            return _cmd_run(args, dry_run=False)
    except MCPError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130
    return 1


# --------------------------------------------------------------------------


def _cmd_discover(args: argparse.Namespace) -> int:
    source = FusionSource(args.fusion_url, timeout=args.timeout,
                          allow_design_edit=not args.fusion_read_only)
    try:
        discovery = source.connect()
        print("Fusion MCP : %s" % args.fusion_url)
        print("Tools (%d): %s" % (len(discovery.tools),
                                  ", ".join(t.get("name", "?") for t in discovery.tools)))
        print("Strategy   : %s" % discovery.strategy)
        for reason in discovery.reasons:
            print("             %s" % reason)
        disc.write_json(os.path.join(args.discovery_dir, "fusion-tools.json"), discovery.tools)
        disc.write_json(os.path.join(args.discovery_dir, "fusion-discovery.json"), discovery.to_dict())
        if discovery.strategy == "none":
            return 3
        raw = source.extract_raw()
        disc.write_json(os.path.join(args.discovery_dir, "design-raw.json"), raw)
        summary = disc.summarize_design(raw)
        disc.write_json(os.path.join(args.discovery_dir, "design-summary.json"), summary)
        print("Design     : %s (%s), %d parameters, %d timeline items, %d bodies"
              % (summary["design"], summary["units"], summary["parameter_count"],
                 summary["timeline_length"], summary["body_count"]))
        if summary["feature_types"]:
            print("Feature types:")
            for ftype, count in summary["feature_types"].items():
                print("  %4d  %s" % (count, ftype))
        return 0
    finally:
        source.close()


def _cmd_run(args: argparse.Namespace, dry_run: bool) -> int:
    source = FusionSource(args.fusion_url, timeout=args.timeout,
                          allow_design_edit=not args.fusion_read_only)
    sink: Optional[SolidCraftSink] = None
    try:
        discovery = source.connect()
        if discovery.strategy == "none":
            raise MCPError(
                "the Fusion MCP server exposes no usable tool; run 'fusion2solid discover'. Tools: %s"
                % ", ".join(t.get("name", "?") for t in discovery.tools)
            )
        design = source.extract_design()
        print("Fusion design %r: %d parameters, %d steps"
              % (design.name, len(design.params), len(design.steps)))

        os.makedirs(args.report_dir, exist_ok=True)
        sink, transport = _make_sink(args)
        print("SolidCraft : %s" % transport)
        sink.connect(design_name="%s-migrated" % (design.name or "design"), create=not dry_run)

        # Two-pass mapping: map once to learn which command ids we touch, ask
        # SolidCraft to describe exactly those, then map again with real schemas.
        first = map_design(design, sink.catalog)
        sink.describe(_touched_commands(first))
        plan = map_design(design, sink.catalog)
        _apply_filters(plan, args.only, args.skip)

        outcomes: List[StepOutcome] = []
        if not dry_run:
            outcomes = sink.apply_plan(plan)
            succeeded = sum(1 for o in outcomes if o.status == "mapped")
            failed = sum(1 for o in outcomes if o.status == "failed")
            print("Replayed %d steps (%d ok, %d failed)" % (len(outcomes), succeeded, failed))

        validation = _validate(sink, design, dry_run)
        if not dry_run and not args.no_save:
            out_path = os.path.abspath(args.out or os.path.join(
                args.report_dir, "%s-migrated.sc" % (design.name or "design")))
            try:
                sink.save(out_path)
                validation["saved_to"] = out_path
                print("Saved    : %s" % out_path)
            except MCPError as exc:
                validation["save_error"] = str(exc)
                print("save failed: %s" % exc, file=sys.stderr)
        if not args.no_screenshot:
            shot = os.path.abspath(os.path.join(args.report_dir, "migrated.png"))
            try:
                if sink.screenshot(shot):
                    validation["screenshot"] = shot
            except MCPError as exc:
                validation["screenshot_error"] = str(exc)

        report = build_report(
            design, plan, outcomes, args.fusion_url, sink.command,
            discovery=discovery.to_dict(), validation=validation, dry_run=dry_run,
        )
        paths = write_report(report, args.report_dir)
        _print_summary(report)
        _warn_on_drift(validation)
        for step in report["steps"]:
            if step["status"] == "failed":
                print("  failed %s %s: %s" % (step["index"], step["name"],
                                              (step["error"] or step["reason"])[:200]))
        print("Report   : %s" % paths["markdown"])
        failed = report["summary"]["failed"]
        return 0 if failed == 0 else 1
    finally:
        if sink is not None:
            sink.close()
        source.close()


# --------------------------------------------------------------------------


def _resolve_port(args: argparse.Namespace) -> int:
    if args.solidcraft_port:
        return args.solidcraft_port
    discovered = control_bridge.discover_port()
    return discovered or DEFAULT_SOLIDCRAFT_PORT


def _find_cli(args: argparse.Namespace) -> Optional[str]:
    if args.solidcraft_cli != DEFAULT_CLI:
        # An explicit path must exist: the user asked for it.
        if os.path.exists(args.solidcraft_cli):
            return args.solidcraft_cli
        raise MCPError("solidcraft-cli not found at %r" % args.solidcraft_cli)
    for candidate in (shutil.which("solidcraft-cli"), shutil.which("solidcraft-cli.exe")):
        if candidate:
            return candidate
    for candidate in (DEFAULT_CLI, r"C:\Program Files (x86)\SolidCraft\solidcraft-cli.exe"):
        if os.path.exists(candidate):
            return candidate
    return None


def _allow_paths(args: argparse.Namespace) -> List[str]:
    # Absolute: a relative path is resolved against the app's working folder.
    paths: List[str] = []
    for path in (args.allow_path or []):
        paths.append(os.path.abspath(path))
    for path in (args.report_dir, os.path.dirname(os.path.abspath(args.out)) if args.out else None):
        if path and os.path.isdir(path):
            absolute = os.path.abspath(path)
            if absolute not in paths:
                paths.append(absolute)
    return paths


def _make_sink(args: argparse.Namespace) -> "tuple[SolidCraftSink, str]":
    port = _resolve_port(args)
    cli = None
    if args.transport in ("auto", "cli"):
        cli = _find_cli(args)

    if args.transport == "cli" and cli is None:
        raise MCPError(
            "solidcraft-cli was not found. Install SolidCraft's CLI, pass --solidcraft-cli PATH, "
            "or use --transport control to drive the running app's control channel."
        )

    use_control = args.transport == "control" or (args.transport == "auto" and cli is None)

    if use_control:
        if control_bridge.read_token(port) is None:
            raise MCPError(
                "cannot reach SolidCraft: solidcraft-cli is not installed and no control token "
                "exists for port %d. Start the SolidCraft app (it writes the token for this "
                "launch), or install solidcraft-cli." % port
            )
        client = control_bridge.ControlMCPClient(
            port=port, timeout=args.timeout, allow_paths=_allow_paths(args))
        sink = SolidCraftSink(client=client, command=["control:%d" % port],
                              timeout=args.timeout, tol_mm=args.tolerance)
        return sink, "control channel 127.0.0.1:%d" % port

    command = [cli, "mcp", "--connect", "127.0.0.1:%d" % port]
    for path in _allow_paths(args):
        command += ["--allow-path", path]
    sink = SolidCraftSink(command, timeout=args.timeout,
                          stderr_path=os.path.join(args.report_dir, "solidcraft-stderr.log"),
                          tol_mm=args.tolerance)
    return sink, "solidcraft-cli mcp --connect 127.0.0.1:%d" % port


def _touched_commands(plan: Plan) -> List[str]:
    ids: List[str] = []
    for command in plan.params:
        ids.append(command.command)
    for step in plan.steps:
        for command in step.commands:
            ids.append(command.command)
    return ids


def _apply_filters(plan: Plan, only: Sequence[str], skip: Sequence[str]) -> None:
    only = [o.lower() for o in only]
    skip = [s.lower() for s in skip]
    for step in plan.steps:
        name = (step.source_name or "").lower()
        if only and not any(o in name for o in only):
            step.status = "skipped"
            step.reason = "filtered out by --only"
        if skip and any(s in name for s in skip):
            step.status = "skipped"
            step.reason = "filtered out by --skip"


def _validate(sink: SolidCraftSink, design: Any, dry_run: bool) -> Dict[str, Any]:
    fusion_volume = None
    volumes = [b.volume for b in design.bodies if b.volume]
    if volumes:
        fusion_volume = sum(volumes)
    validation: Dict[str, Any] = {
        "fusion_bodies": [b.to_dict() for b in design.bodies],
        "fusion_total_volume_mm3": fusion_volume,
    }
    if dry_run:
        return validation

    measure = sink.measure()
    if isinstance(measure, dict):
        total = measure.get("total") or {}
        sc_volume = total.get("volume_mm3")
        validation["solidcraft_total_volume_mm3"] = sc_volume
        if fusion_volume and sc_volume:
            delta = abs(float(sc_volume) - float(fusion_volume))
            validation["volume_delta_mm3"] = delta
            validation["volume_relative_delta"] = delta / float(fusion_volume)
    inspect = sink.inspect()
    if isinstance(inspect, dict):
        bodies = inspect.get("bodies") or []
        validation["solidcraft_body_count"] = len(bodies)
        validation["solidcraft_bodies"] = [
            {"name": b.get("name"), "volume": b.get("volume")} for b in bodies if isinstance(b, dict)
        ]
        timeline = inspect.get("timeline") or []
        validation["solidcraft_timeline_length"] = len(timeline)
    return validation


def _print_summary(report: Dict[str, Any]) -> None:
    summary = report["summary"]
    print(
        "Summary  : %d mapped, %d skipped, %d failed (of %d steps)"
        % (summary["mapped"], summary["skipped"], summary["failed"], summary["timeline_steps"])
    )


DRIFT_WARN = 0.01  # 1%


def _warn_on_drift(validation: Dict[str, Any]) -> None:
    """Say so when the migrated model does not measure up to the original."""
    delta = validation.get("volume_relative_delta")
    if isinstance(delta, (int, float)) and delta > DRIFT_WARN:
        print(
            "warning  : migrated volume differs from Fusion by %.1f%% "
            "(Fusion %s mm^3, SolidCraft %s mm^3); the geometry is not faithful "
            "- see the report" % (
                delta * 100.0,
                _round(validation.get("fusion_total_volume_mm3")),
                _round(validation.get("solidcraft_total_volume_mm3")),
            ),
            file=sys.stderr,
        )


def _round(value: Any) -> str:
    return "%.1f" % value if isinstance(value, (int, float)) else str(value)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
