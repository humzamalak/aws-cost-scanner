import datetime as dt

from scanner import unused


def test_unattached_volume_is_found(aws):
    ec2 = aws.client("ec2")
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")
    [w] = unused.unattached_volumes("us-east-1", aws)
    assert w.check == "EBS_UNATTACHED"
    assert w.monthly_usd == 8.0  # 100 GiB x 0.08


def test_attached_volume_is_ignored(aws):
    ec2 = aws.client("ec2")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="t3.micro")
    assert unused.unattached_volumes("us-east-1", aws) == []


def test_unassociated_eip_is_found_and_associated_is_not(aws):
    ec2 = aws.client("ec2")
    ec2.allocate_address(Domain="vpc")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    instance = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    used = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.associate_address(AllocationId=used, InstanceId=instance)
    found = unused.unassociated_eips("us-east-1", aws)
    assert len(found) == 1
    assert found[0].monthly_usd == 3.65


def test_old_snapshot_is_found(aws, monkeypatch):
    ec2 = aws.client("ec2")
    vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50)["VolumeId"]
    snap = ec2.create_snapshot(VolumeId=vol)["SnapshotId"]
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=120)
    monkeypatch.setattr(unused, "_now", lambda: future)
    found = {w.resource_id: w for w in unused.old_snapshots("us-east-1", aws, min_age_days=90)}
    assert found[snap].check == "SNAPSHOT_OLD"
    assert found[snap].monthly_usd == 2.5


def test_new_snapshot_is_ignored(aws):
    ec2 = aws.client("ec2")
    vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50)["VolumeId"]
    snap = ec2.create_snapshot(VolumeId=vol)["SnapshotId"]
    assert snap not in {w.resource_id for w in unused.old_snapshots("us-east-1", aws, min_age_days=90)}


def test_stopped_instance_is_found(aws, monkeypatch):
    ec2 = aws.client("ec2")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    iid = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[iid])
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    monkeypatch.setattr(unused, "_now", lambda: future)
    found = unused.stopped_instances("us-east-1", aws, min_age_days=14)
    assert [w.resource_id for w in found] == [iid]


def test_recently_stopped_instance_is_ignored(aws):
    ec2 = aws.client("ec2")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    iid = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[iid])
    assert unused.stopped_instances("us-east-1", aws, min_age_days=14) == []


def test_stopped_at_parsing():
    when = unused._stopped_at("User initiated (2026-01-02 03:04:05 GMT)")
    assert when == dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    assert unused._stopped_at("User initiated (2026-01-02 03:04:05 UTC)") == when
    assert unused._stopped_at("") is None
    assert unused._stopped_at("User initiated (garbage GMT)") is None


def test_load_balancer_without_targets_is_found(aws):
    ec2 = aws.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=f"us-east-1{az}")["Subnet"][
            "SubnetId"
        ]
        for i, az in enumerate("ab")
    ]
    aws.client("elbv2").create_load_balancer(Name="orphan", Subnets=subnets)
    [w] = unused.idle_load_balancers("us-east-1", aws)
    assert w.resource_id == "orphan"


def test_log_group_without_retention(aws):
    logs = aws.client("logs")
    logs.create_log_group(logGroupName="/forever")
    logs.create_log_group(logGroupName="/thirty")
    logs.put_retention_policy(logGroupName="/thirty", retentionInDays=30)
    found = unused.log_groups_without_retention("us-east-1", aws)
    assert [w.resource_id for w in found] == ["/forever"]


def test_idle_nat_gateway_is_found(aws):
    ec2 = aws.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.create_nat_gateway(SubnetId=subnet, AllocationId=eip)
    found = unused.idle_nat_gateways("us-east-1", aws)
    assert [w.check for w in found] == ["NAT_IDLE"]
