"""Lambda entry point. Runs on an EventBridge Scheduler schedule.

Environment variables
  REPORT_BUCKET       S3 bucket for reports (required)
  SNS_TOPIC_ARN       topic for the e-mail summary (required)
  REGIONS             comma-separated regions to scan, or "all" (default: the Lambda region)
  SNAPSHOT_AGE_DAYS   flag snapshots older than this (default 90)
  STOPPED_AGE_DAYS    flag instances stopped longer than this (default 14)
  INCLUDE_COSTS       "false" to skip Cost Explorer (default "true")
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any

import boto3

from scanner import costs, report, unused

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def _regions(session: boto3.Session) -> list[str]:
    value = os.environ.get("REGIONS", "").strip()
    if value == "all":
        ec2 = session.client("ec2")
        return sorted(r["RegionName"] for r in ec2.describe_regions(AllRegions=False)["Regions"])
    if value:
        return [r.strip() for r in value.split(",") if r.strip()]
    return [session.region_name]


def lambda_handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    snapshot_age = int(os.environ.get("SNAPSHOT_AGE_DAYS", "90"))
    stopped_age = int(os.environ.get("STOPPED_AGE_DAYS", "14"))

    waste: list[unused.Waste] = []
    errors: list[str] = []
    for region in _regions(session):
        try:
            waste += unused.scan_region(region, session, snapshot_age, stopped_age)
        except Exception as exc:  # noqa: BLE001 - one bad region must not stop the report
            logger.exception("scan failed in %s", region)
            errors.append(f"{region}: {exc}")

    spend = None
    if os.environ.get("INCLUDE_COSTS", "true").lower() == "true":
        try:
            spend = costs.summarise(session.client("ce", region_name="us-east-1"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cost Explorer failed")
            errors.append(f"cost explorer: {exc}")

    now = dt.datetime.now(dt.UTC)
    stamp = now.strftime("%Y-%m-%dT%H%MZ")
    md = report.markdown(account, now.strftime("%Y-%m-%d %H:%M"), spend, waste)
    if errors:
        md += "\n## Errors\n\n" + "\n".join(f"- {e}" for e in errors) + "\n"

    bucket = os.environ["REPORT_BUCKET"]
    key_md = f"reports/{now:%Y/%m}/{stamp}.md"
    s3 = session.client("s3")
    s3.put_object(Bucket=bucket, Key=key_md, Body=md.encode(), ContentType="text/markdown")
    s3.put_object(
        Bucket=bucket,
        Key=key_md.replace(".md", ".json"),
        Body=json.dumps(
            {
                "account": account,
                "generated": stamp,
                "waste": [w.to_dict() for w in waste],
                "spend": spend.__dict__ if spend else None,
                "errors": errors,
            },
            default=str,
        ).encode(),
        ContentType="application/json",
    )

    subject, body = report.email(account, spend, waste, f"s3://{bucket}/{key_md}")
    session.client("sns").publish(TopicArn=os.environ["SNS_TOPIC_ARN"], Subject=subject, Message=body)

    result = {"findings": len(waste), "monthly_usd": round(sum(w.monthly_usd for w in waste), 2), "report": key_md}
    logger.info(json.dumps(result))
    return result
