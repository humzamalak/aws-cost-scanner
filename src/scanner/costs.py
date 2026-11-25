"""Spend summary from Cost Explorer. The Cost Explorer API is global (us-east-1).

Cost Explorer charges per API request (see the AWS pricing page), so the
scanner makes a fixed, small number of calls per run.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SpendSummary:
    month_to_date: float
    last_month_same_period: float
    forecast_month_end: float | None
    top_services: list[tuple[str, float]] = field(default_factory=list)
    currency: str = "USD"

    @property
    def change_pct(self) -> float | None:
        if not self.last_month_same_period:
            return None
        return round((self.month_to_date - self.last_month_same_period) / self.last_month_same_period * 100, 1)


def _total(resp: dict[str, Any]) -> tuple[float, str]:
    total, unit = 0.0, "USD"
    for r in resp.get("ResultsByTime", []):
        amount = r["Total"]["UnblendedCost"]
        total += float(amount["Amount"])
        unit = amount.get("Unit", unit)
    return round(total, 2), unit


def summarise(ce, today: dt.date | None = None, top_n: int = 5) -> SpendSummary:
    today = today or dt.date.today()
    run_date = today
    first = today.replace(day=1)
    if today == first:
        # On the 1st there is no completed day this month yet. Report the previous month.
        today = first
        first = (first - dt.timedelta(days=1)).replace(day=1)
    prev_first = (first - dt.timedelta(days=1)).replace(day=1)
    days_in = (today - first).days
    prev_end = min(prev_first + dt.timedelta(days=days_in), first)

    mtd, unit = _total(
        ce.get_cost_and_usage(
            TimePeriod={"Start": first.isoformat(), "End": today.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
    )
    prev, _ = _total(
        ce.get_cost_and_usage(
            TimePeriod={"Start": prev_first.isoformat(), "End": prev_end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
    )

    by_service = ce.get_cost_and_usage(
        TimePeriod={"Start": first.isoformat(), "End": today.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    services: dict[str, float] = {}
    for r in by_service.get("ResultsByTime", []):
        for g in r.get("Groups", []):
            services[g["Keys"][0]] = services.get(g["Keys"][0], 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
    top = sorted(((k, round(v, 2)) for k, v in services.items() if v > 0), key=lambda x: -x[1])[:top_n]

    next_month = (first + dt.timedelta(days=32)).replace(day=1)
    forecast = None
    tomorrow = run_date + dt.timedelta(days=1)
    if run_date != first and tomorrow < next_month:
        try:
            f = ce.get_cost_forecast(
                TimePeriod={"Start": tomorrow.isoformat(), "End": next_month.isoformat()},
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY",
            )
            forecast = round(mtd + float(f["Total"]["Amount"]), 2)
        except Exception:  # noqa: BLE001 - new accounts have no forecast data yet
            forecast = None

    return SpendSummary(mtd, prev, forecast, top, unit)
