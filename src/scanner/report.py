"""Turn scan results into a Markdown report and a short e-mail summary."""

from __future__ import annotations

from scanner.costs import SpendSummary
from scanner.unused import Waste


def markdown(account: str, generated: str, spend: SpendSummary | None, waste: list[Waste]) -> str:
    lines = [f"# AWS cost report for account {account}", "", f"Generated {generated} UTC.", ""]
    if spend:
        change = f" ({spend.change_pct:+.1f}% against the same days last month)" if spend.change_pct is not None else ""
        lines += [
            "## Spend",
            "",
            f"- Month to date: **{spend.month_to_date:.2f} {spend.currency}**{change}",
            f"- Forecast for the full month: {spend.forecast_month_end:.2f} {spend.currency}"
            if spend.forecast_month_end is not None
            else "- Forecast: not available yet",
            "",
            "| Service | Month to date |",
            "| --- | ---: |",
        ]
        lines += [f"| {name} | {amount:.2f} |" for name, amount in spend.top_services] or ["| none | 0.00 |"]
        lines.append("")

    total = sum(w.monthly_usd for w in waste)
    lines += [
        "## Probably unused resources",
        "",
        f"{len(waste)} found. Estimated cost if left running: **{total:.2f} USD per month** "
        "(us-east-1 list prices, upper bound).",
        "",
    ]
    if waste:
        lines += [
            "| Check | Region | Resource | Detail | USD/month | Suggested action |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
        for w in sorted(waste, key=lambda x: -x.monthly_usd):
            lines.append(
                f"| {w.check} | {w.region} | `{w.resource_id}` | {w.detail} | {w.monthly_usd:.2f} | {w.action} |"
            )
    lines += ["", "The scanner is read-only. Check each resource before you delete it."]
    return "\n".join(lines) + "\n"


def email(account: str, spend: SpendSummary | None, waste: list[Waste], report_uri: str) -> tuple[str, str]:
    total = sum(w.monthly_usd for w in waste)
    subject = f"AWS cost report {account}: {len(waste)} unused resources, ~{total:.0f} USD/month"
    body = []
    if spend:
        body.append(f"Month to date: {spend.month_to_date:.2f} {spend.currency}")
        if spend.change_pct is not None:
            body.append(f"Change against the same days last month: {spend.change_pct:+.1f}%")
        if spend.forecast_month_end is not None:
            body.append(f"Forecast for the month: {spend.forecast_month_end:.2f} {spend.currency}")
        body.append("")
    body.append(f"Probably unused resources: {len(waste)} (about {total:.2f} USD per month)")
    for w in sorted(waste, key=lambda x: -x.monthly_usd)[:10]:
        body.append(f"  {w.monthly_usd:>8.2f}  {w.check:<18} {w.region:<12} {w.resource_id}")
    if len(waste) > 10:
        body.append(f"  ... and {len(waste) - 10} more")
    body += ["", f"Full report: {report_uri}"]
    return subject[:100], "\n".join(body)
