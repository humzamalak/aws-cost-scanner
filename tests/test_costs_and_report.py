import datetime as dt
import json

from conftest import FakeCostExplorer

from scanner import costs, handler, report
from scanner.unused import Waste


def test_summary_compares_same_days_last_month():
    ce = FakeCostExplorer()
    s = costs.summarise(ce, today=dt.date(2026, 11, 25))
    assert s.month_to_date == 120.5
    assert s.last_month_same_period == 100.0
    assert s.change_pct == 20.5
    assert s.forecast_month_end == 200.5
    assert s.top_services == [("Amazon EC2", 70.0), ("Amazon S3", 20.5)]
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-11-01", "End": "2026-11-25"}
    assert ce.calls[1]["TimePeriod"] == {"Start": "2026-10-01", "End": "2026-10-25"}


def test_first_of_month_reports_previous_month():
    ce = FakeCostExplorer()
    costs.summarise(ce, today=dt.date(2026, 12, 1))
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-11-01", "End": "2026-12-01"}


def test_missing_forecast_is_none():
    s = costs.summarise(FakeCostExplorer(forecast=None), today=dt.date(2026, 11, 25))
    assert s.forecast_month_end is None


WASTE = [
    Waste("EBS_UNATTACHED", "us-east-1", "vol-1", "100 GiB gp3", 8.0, "Delete"),
    Waste("EIP_UNUSED", "eu-west-2", "eipalloc-1", "not associated", 3.65, "Release"),
]


def test_markdown_report():
    spend = costs.SpendSummary(120.5, 100.0, 200.5, [("Amazon EC2", 70.0)])
    md = report.markdown("111122223333", "2026-11-25 08:00", spend, WASTE)
    assert "+20.5%" in md
    assert "11.65 USD per month" in md
    assert md.index("vol-1") < md.index("eipalloc-1")  # sorted by cost


def test_email_summary():
    subject, body = report.email("111122223333", None, WASTE, "s3://b/k.md")
    assert subject.startswith("AWS cost report 111122223333: 2 unused resources")
    assert "s3://b/k.md" in body


def test_handler_writes_report_and_sends_email(aws, monkeypatch):
    s3 = aws.client("s3")
    s3.create_bucket(Bucket="reports")
    sns = aws.client("sns")
    topic = sns.create_topic(Name="cost")["TopicArn"]
    aws.client("ec2").create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp2")
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setenv("SNS_TOPIC_ARN", topic)
    monkeypatch.setenv("REGIONS", "us-east-1")
    monkeypatch.setenv("INCLUDE_COSTS", "false")

    result = handler.lambda_handler({})

    assert result["findings"] == 1
    assert result["monthly_usd"] == 1.0
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="reports")["Contents"]]
    assert any(k.endswith(".md") for k in keys) and any(k.endswith(".json") for k in keys)
    data = json.loads(s3.get_object(Bucket="reports", Key=[k for k in keys if k.endswith(".json")][0])["Body"].read())
    assert data["waste"][0]["check"] == "EBS_UNATTACHED"


def test_handler_keeps_going_when_a_region_fails(aws, monkeypatch):
    aws.client("s3").create_bucket(Bucket="reports")
    topic = aws.client("sns").create_topic(Name="cost")["TopicArn"]
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setenv("SNS_TOPIC_ARN", topic)
    monkeypatch.setenv("REGIONS", "us-east-1,eu-west-2")
    monkeypatch.setenv("INCLUDE_COSTS", "false")

    calls = []

    def flaky(region, *args):
        calls.append(region)
        if region == "eu-west-2":
            raise RuntimeError("AccessDenied")
        return []

    monkeypatch.setattr(handler.unused, "scan_region", flaky)
    assert handler.lambda_handler({})["findings"] == 0
    assert calls == ["us-east-1", "eu-west-2"]
