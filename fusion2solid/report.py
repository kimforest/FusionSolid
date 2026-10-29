"""Build the migration report (machine JSON + human Markdown)."""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Dict, List, Sequence

from .ir import Design
from .mapper import Plan
from .solidcraft_sink import StepOutcome


def build_report(
    design: Design,
    plan: Plan,
    outcomes: Sequence[StepOutcome],
    fusion_endpoint: str,
    solidcraft_command: Sequence[str],
    discovery: Optional[Dict[str, Any]] = None,
    validation: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    by_symbol = {o.symbol: o for o in outcomes}
    steps: List[Dict[str, Any]] = []

    outcome_index = 0
    for step in plan.steps:
        outcome = by_symbol.get(step.symbol)
        # Parameter pseudo-steps have unique symbols; match them positionally too.
        status = outcome.status if outcome else step.status
        steps.append(
            {
                "index": step.index,
                "symbol": step.symbol,
                "type": step.source_type,
                "name": step.source_name,
                "status": status,
                "reason": (outcome.reason if outcome else step.reason) or step.reason,
                "error": outcome.error if outcome else "",
                "commands": [c.to_dict() for c in step.commands],
                "notes": step.notes,
                "suppressed": step.suppressed,
            }
        )
        outcome_index += 1

    param_failures = [o for o in outcomes if o.symbol.startswith("param:") and o.status == "failed"]
    param_pseudosteps = [
        {"name": c.params.get("name", "?"), "solidcraft": c.to_dict()}
        for c in plan.params
    ]

    counts = {"mapped": 0, "skipped": 0, "failed": 0}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    for failure in param_failures:
        counts["failed"] += 1

    report: Dict[str, Any] = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "dry_run": dry_run,
        "fusion": {"design": design.name, "endpoint": fusion_endpoint, "discovery": discovery or {}},
        "solidcraft": {"command": list(solidcraft_command)},
        "summary": {
            "timeline_steps": len(steps),
            "parameters": len(plan.params),
            "mapped": counts.get("mapped", 0),
            "skipped": counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
            "parameter_failures": len(param_failures),
        },
        "parameters": param_pseudosteps,
        "unreadable_inputs": (design.metadata or {}).get("timeline_unreadable") or [],
        "steps": steps,
        "unmapped": [
            {"type": s["type"], "name": s["name"], "reason": s["reason"] or s["error"]}
            for s in steps
            if s["status"] != "mapped"
        ],
        "validation": validation or {},
    }
    return report


def write_report(report: Dict[str, Any], report_dir: str) -> Dict[str, str]:
    os.makedirs(report_dir, exist_ok=True)
    json_path = os.path.join(report_dir, "report.json")
    md_path = os.path.join(report_dir, "report.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    return {"json": json_path, "markdown": md_path}


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines: List[str] = []
    lines.append("# Fusion -> SolidCraft migration report")
    lines.append("")
    lines.append("- Generated: %s" % report.get("generated_at", ""))
    lines.append("- Fusion design: %s" % report.get("fusion", {}).get("design", ""))
    lines.append("- Fusion endpoint: %s" % report.get("fusion", {}).get("endpoint", ""))
    lines.append("- Mode: %s" % ("dry run" if report.get("dry_run") else "migrate"))
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| metric | count |")
    lines.append("| --- | --- |")
    lines.append("| timeline steps | %s |" % summary.get("timeline_steps", 0))
    lines.append("| mapped | %s |" % summary.get("mapped", 0))
    lines.append("| skipped | %s |" % summary.get("skipped", 0))
    lines.append("| failed | %s |" % summary.get("failed", 0))
    lines.append("| parameters | %s |" % summary.get("parameters", 0))
    lines.append("")

    validation = report.get("validation") or {}
    if validation:
        lines.append("## Validation")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(validation, indent=2, default=str))
        lines.append("```")
        lines.append("")

    lines.append("## Steps")
    lines.append("")
    lines.append("| # | type | name | status | detail |")
    lines.append("| --- | --- | --- | --- | --- |")
    for step in report.get("steps", []):
        detail = step.get("error") or step.get("reason") or "; ".join(step.get("notes", []) or [])
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (step.get("index"), step.get("type"), step.get("name"),
               step.get("status"), _escape(detail))
        )
    lines.append("")

    if report.get("unmapped"):
        lines.append("## Unmapped")
        lines.append("")
        for item in report["unmapped"]:
            lines.append("- `%s` %s - %s" % (item.get("type"), item.get("name"), item.get("reason")))
        lines.append("")

    if report.get("unreadable_inputs"):
        lines.append("## Inputs Fusion did not expose")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(report["unreadable_inputs"], indent=2, default=str))
        lines.append("```")
        lines.append("")

    if report.get("parameters"):
        lines.append("## Parameters")
        lines.append("")
        lines.append("| name | SolidCraft command |")
        lines.append("| --- | --- |")
        for param in report["parameters"]:
            lines.append("| %s | `%s` |" % (param.get("name"), json.dumps(param.get("solidcraft"))))
        lines.append("")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")
