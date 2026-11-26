import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.Session(region_name="us-east-1")


class FakeCostExplorer:
    """Returns fixed Cost Explorer answers, so tests do not depend on moto's CE support."""

    def __init__(self, mtd="120.50", prev="100.00", forecast="80.00"):
        self.mtd, self.prev, self.forecast, self.calls = mtd, prev, forecast, []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("GroupBy"):
            groups = [
                {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "70.0", "Unit": "USD"}}},
                {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "20.5", "Unit": "USD"}}},
                {"Keys": ["Tax"], "Metrics": {"UnblendedCost": {"Amount": "0", "Unit": "USD"}}},
            ]
            return {"ResultsByTime": [{"Groups": groups}]}
        amount = self.mtd if len(self.calls) == 1 else self.prev
        return {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}]}

    def get_cost_forecast(self, **kwargs):
        if self.forecast is None:
            raise RuntimeError("DataUnavailableException")
        return {"Total": {"Amount": self.forecast, "Unit": "USD"}}
