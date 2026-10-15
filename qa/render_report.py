"""Compact, escaped Markdown from data; reports never execute artifact HTML."""
def clean(value):
    return str(value).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")


def render(report):
    coverage = report["coverage"]
    lines = [f"# QA {report['status']}", "", f"Tested SHA: `{report['tested_sha']}`",
             f"Coverage: {coverage['completed']}/{coverage['required']} required instances.",
             f"Plan: `{report['plan_hash']}`", "",
             "| Case | Layer | Functional | Validity | Performance |", "|---|---|---|---|---|"]
    for row in report["cases"]:
        validity = row["validity"]
        if isinstance(validity, dict):
            validity = f"functional={validity['functional']}, performance={validity['performance']}"
        lines.append("| " + " | ".join(clean(v) for v in (row["case_id"], row["layer"], row["functional"], validity, row["performance"])) + " |")
    for title, key in (("Failures", "failures"), ("Blocked evidence", "blocked"), ("Limits and exemptions", "notes")):
        if report[key]:
            lines += ["", f"## {title}", ""] + [f"- {clean(item)}" for item in report[key]]
    for row in report["cases"]:
        metrics = row.get("performance_metrics", {})
        details = row.get("assertion_details", [])
        if not metrics and not details:
            continue
        lines += ["", f"## {clean(row['case_id'])}", "",
                  f"Attribution: {clean(row.get('attribution', 'UNATTRIBUTED'))}; run: `{clean(row.get('run_id', 'NOT_RUN'))}`."]
        lines += [f"- {clean(item)}" for item in details]
        for name, metric in metrics.items():
            if isinstance(metric, dict) and "interval" in metric:
                lines.append(f"- {clean(name)}: {clean(metric.get('blocks'))} complete blocks; "
                             f"degradation scale={clean(metric.get('scale'))}; simultaneous interval "
                             f"{clean(metric['interval'])}; budget={clean(metric.get('budget'))}; "
                             f"{clean(metric.get('status'))}. Raw metric unit: {clean(metric.get('unit'))}.")
            else:
                lines.append(f"- {clean(name)}: {clean(metric) if metric is not None else 'unavailable'} (descriptive observation).")
    lines += ["", "## Replay and input identities", "",
              "Use the original frozen plan and tested checkout. Each attempt needs a new output directory; "
              "integration/performance also require the same reviewed node inputs and physical-domain lease.", "",
              "```sh", "python -m qa run-plan --plan plan.json --output qa/results/new-attempt", "```", "",
              "| Case | Build manifest | Environment | Configuration |", "|---|---|---|---|"]
    for row in report["cases"]:
        if "build_manifest_hash" in row:
            lines.append("| " + " | ".join(clean(row[key]) for key in
                         ("case_id", "build_manifest_hash", "environment_hash", "config_hash")) + " |")
    lines += ["", clean(report.get("scope", "")), ""]
    return "\n".join(lines)
