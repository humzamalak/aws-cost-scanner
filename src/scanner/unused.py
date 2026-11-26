"""Find resources that cost money but are probably not used.

Every check is read-only. Nothing is deleted or changed.
Monthly cost estimates use the list prices in PRICES (us-east-1, on-demand).
Prices differ per region; change PRICES or treat the numbers as a guide.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

# USD list prices (us-east-1). Check https://aws.amazon.com/pricing/ before you rely on them.
PRICES = {
    "ebs_gb_month": {
        "gp2": 0.10,
        "gp3": 0.08,
        "io1": 0.125,
        "io2": 0.125,
        "st1": 0.045,
        "sc1": 0.015,
        "standard": 0.05,
    },
    "snapshot_gb_month": 0.05,
    "public_ipv4_hour": 0.005,
    "alb_hour": 0.0225,
    "nlb_hour": 0.0225,
    "nat_hour": 0.045,
}
HOURS_PER_MONTH = 730


@dataclass
class Waste:
    check: str
    region: str
    resource_id: str
    detail: str
    monthly_usd: float
    action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _age_days(when: dt.datetime | None) -> int:
    return (_now() - when).days if when else 0


def unattached_volumes(region: str, session: boto3.Session) -> list[Waste]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for page in ec2.get_paginator("describe_volumes").paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for v in page["Volumes"]:
            price = PRICES["ebs_gb_month"].get(v["VolumeType"], 0.10)
            out.append(
                Waste(
                    "EBS_UNATTACHED",
                    region,
                    v["VolumeId"],
                    f"{v['Size']} GiB {v['VolumeType']}, not attached, "
                    f"created {_age_days(v.get('CreateTime'))} days ago",
                    round(v["Size"] * price, 2),
                    "Snapshot it if needed, then delete the volume.",
                )
            )
    return out


def unassociated_eips(region: str, session: boto3.Session) -> list[Waste]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for a in ec2.describe_addresses()["Addresses"]:
        if a.get("AssociationId"):
            continue
        out.append(
            Waste(
                "EIP_UNUSED",
                region,
                a.get("AllocationId", a.get("PublicIp", "unknown")),
                f"Elastic IP {a.get('PublicIp')} is not associated",
                round(PRICES["public_ipv4_hour"] * HOURS_PER_MONTH, 2),
                "Release the address if nothing needs it.",
            )
        )
    return out


def old_snapshots(region: str, session: boto3.Session, min_age_days: int) -> list[Waste]:
    ec2 = session.client("ec2", region_name=region)
    in_use = set()
    for image in ec2.describe_images(Owners=["self"])["Images"]:
        for m in image.get("BlockDeviceMappings", []):
            if "Ebs" in m and m["Ebs"].get("SnapshotId"):
                in_use.add(m["Ebs"]["SnapshotId"])
    out = []
    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            age = _age_days(s.get("StartTime"))
            if age < min_age_days or s["SnapshotId"] in in_use:
                continue
            # Snapshots are incremental, so the real stored size is often smaller. This is an upper bound.
            out.append(
                Waste(
                    "SNAPSHOT_OLD",
                    region,
                    s["SnapshotId"],
                    f"{s['VolumeSize']} GiB snapshot, {age} days old, not used by any AMI",
                    round(s["VolumeSize"] * PRICES["snapshot_gb_month"], 2),
                    "Delete it, or move it to the EBS Snapshots Archive tier.",
                )
            )
    return out


def _stopped_at(reason: str) -> dt.datetime | None:
    """StateTransitionReason looks like 'User initiated (2026-01-01 10:00:00 GMT)'."""
    match = re.search(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:GMT|UTC)\)", reason)
    if not match:
        return None
    try:
        stamp = match.group(1)
        return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def stopped_instances(region: str, session: boto3.Session, min_age_days: int) -> list[Waste]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
    )
    for page in pages:
        for reservation in page["Reservations"]:
            for i in reservation["Instances"]:
                stopped_at = _stopped_at(i.get("StateTransitionReason", ""))
                if stopped_at and _age_days(stopped_at) < min_age_days:
                    continue
                vols = [m["Ebs"]["VolumeId"] for m in i.get("BlockDeviceMappings", []) if "Ebs" in m]
                cost = 0.0
                if vols:
                    for v in ec2.describe_volumes(VolumeIds=vols)["Volumes"]:
                        cost += v["Size"] * PRICES["ebs_gb_month"].get(v["VolumeType"], 0.10)
                since = f"for {_age_days(stopped_at)} days" if stopped_at else "(stop time unknown)"
                out.append(
                    Waste(
                        "EC2_STOPPED",
                        region,
                        i["InstanceId"],
                        f"stopped {since}, {len(vols)} EBS volume(s) still billed",
                        round(cost, 2),
                        "Create an AMI if needed, then terminate the instance.",
                    )
                )
    return out


def idle_load_balancers(region: str, session: boto3.Session) -> list[Waste]:
    elb = session.client("elbv2", region_name=region)
    out = []
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            try:
                groups = elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TargetGroupNotFound":
                    raise
                groups = []
            healthy_targets = 0
            for g in groups:
                health = elb.describe_target_health(TargetGroupArn=g["TargetGroupArn"])["TargetHealthDescriptions"]
                healthy_targets += len(health)
            if healthy_targets:
                continue
            kind = "nlb_hour" if lb.get("Type") == "network" else "alb_hour"
            out.append(
                Waste(
                    "LB_NO_TARGETS",
                    region,
                    lb["LoadBalancerName"],
                    f"{lb.get('Type', 'application')} load balancer with no registered targets",
                    round(PRICES[kind] * HOURS_PER_MONTH, 2),
                    "Delete the load balancer if no service uses it.",
                )
            )
    return out


def idle_nat_gateways(region: str, session: boto3.Session, days: int = 7) -> list[Waste]:
    ec2 = session.client("ec2", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    out = []
    gateways = ec2.describe_nat_gateways(Filter=[{"Name": "state", "Values": ["available"]}])["NatGateways"]
    for nat in gateways:
        stats = cw.get_metric_statistics(
            Namespace="AWS/NATGateway",
            MetricName="BytesOutToDestination",
            Dimensions=[{"Name": "NatGatewayId", "Value": nat["NatGatewayId"]}],
            StartTime=_now() - dt.timedelta(days=days),
            EndTime=_now(),
            Period=86400,
            Statistics=["Sum"],
        )["Datapoints"]
        if sum(p["Sum"] for p in stats) > 0:
            continue
        out.append(
            Waste(
                "NAT_IDLE",
                region,
                nat["NatGatewayId"],
                f"no traffic in the last {days} days",
                round(PRICES["nat_hour"] * HOURS_PER_MONTH, 2),
                "Delete the NAT gateway and release its Elastic IP.",
            )
        )
    return out


def log_groups_without_retention(region: str, session: boto3.Session) -> list[Waste]:
    logs = session.client("logs", region_name=region)
    out = []
    for page in logs.get_paginator("describe_log_groups").paginate():
        for g in page["logGroups"]:
            if g.get("retentionInDays"):
                continue
            gb = g.get("storedBytes", 0) / 1024**3
            out.append(
                Waste(
                    "LOGS_NO_RETENTION",
                    region,
                    g["logGroupName"],
                    f"never expires, {gb:.2f} GB stored",
                    round(gb * 0.03, 2),
                    "Set a retention period (for example 30 or 90 days).",
                )
            )
    return out


def scan_region(region: str, session: boto3.Session, snapshot_age_days: int, stopped_age_days: int) -> list[Waste]:
    found: list[Waste] = []
    found += unattached_volumes(region, session)
    found += unassociated_eips(region, session)
    found += old_snapshots(region, session, snapshot_age_days)
    found += stopped_instances(region, session, stopped_age_days)
    found += idle_load_balancers(region, session)
    found += idle_nat_gateways(region, session)
    found += log_groups_without_retention(region, session)
    return found
