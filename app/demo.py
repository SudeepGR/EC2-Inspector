"""
Demo mode: a built-in, fully SYNTHETIC fleet so the dashboard works on any
laptop with no AWS account, credentials or network.

Enable with the environment variable DEMO_MODE=1. The fake clients below mimic
only the boto3 calls that ``Ec2Inspector`` makes (EC2, SSM, CloudWatch), so the
real health rules and the real dashboard run unchanged on top of them.

Nothing here is real: names, IDs, IPs and numbers are invented. The fleet is
built to show every verdict (HEALTHY, WARNING, CRITICAL, STOPPED, PENDING) and
the common issues (SSM lost, scheduled event, impaired checks, missing tags,
high and idle CPU).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from botocore.exceptions import ClientError

ACCOUNT_ID = "123456789012"  # obviously fake

DEMO_REGIONS = ["ap-south-1", "eu-west-1", "us-east-1"]
DEMO_DEFAULT_REGION = "us-east-1"

# Compact fleet description. Each row:
# name, type, platform, state, days_up, tags, system, instance, ssm, event, cpu(avg, swing), volumes, imds, arch
LINUX, WINDOWS = "Linux", "Windows"
ONLINE, LOST, NONE = "Online", "ConnectionLost", None


def _tags(name: str, env: str | None = None, owner: str | None = None, group: str | None = None) -> dict[str, str]:
    tags = {"Name": name}
    if env:
        tags["Environment"] = env
    if owner:
        tags["Owner"] = owner
    if group:
        tags["PatchGroup"] = group
    return tags


# fmt: off
FLEET: dict[str, list[dict[str, Any]]] = {
    "us-east-1": [
        dict(name="web-prod-01", type="t3.large", os=LINUX, state="running", days=41, tags=_tags("web-prod-01", "prod", "platform-team", "A"), sys="ok", inst="ok", ssm=ONLINE, cpu=(34, 12), vols=[(30, "gp3", True)], imds="required", ip="10.0.1.11", public="54.210.18.41"),
        dict(name="web-prod-02", type="t3.large", os=LINUX, state="running", days=41, tags=_tags("web-prod-02", "prod", "platform-team", "A"), sys="ok", inst="ok", ssm=ONLINE, cpu=(41, 14), vols=[(30, "gp3", True)], imds="required", ip="10.0.1.12", public="54.210.18.42"),
        dict(name="web-prod-03", type="t3.large", os=LINUX, state="running", days=41, tags=_tags("web-prod-03", "prod", "platform-team", "A"), sys="ok", inst="ok", ssm=LOST, cpu=(38, 11), vols=[(30, "gp3", True)], imds="required", ip="10.0.1.13", public="54.210.18.43"),
        dict(name="api-prod-01", type="m5.xlarge", os=LINUX, state="running", days=12, tags=_tags("api-prod-01", "prod", "platform-team", "A"), sys="ok", inst="impaired", ssm=LOST, cpu=(3, 1), vols=[(50, "gp3", True)], imds="required", ip="10.0.2.21"),
        dict(name="db-prod-01", type="r5.2xlarge", os=LINUX, state="running", days=96, tags=_tags("db-prod-01", "prod", "dba-team", "C"), sys="ok", inst="ok", ssm=ONLINE, cpu=(72, 10), vols=[(100, "gp3", True), (500, "io2", True)], imds="required", ip="10.0.3.31"),
        dict(name="db-prod-02", type="r5.2xlarge", os=LINUX, state="running", days=96, tags=_tags("db-prod-02", "prod", "dba-team", "C"), sys="ok", inst="ok", ssm=ONLINE, event=("instance-retirement", "The instance is running on degraded hardware", 9), cpu=(58, 9), vols=[(100, "gp3", True), (500, "io2", True)], imds="required", ip="10.0.3.32"),
        dict(name="cache-prod-01", type="r6g.large", os=LINUX, state="running", days=63, tags=_tags("cache-prod-01", "prod", "platform-team", "A"), sys="ok", inst="ok", ssm=ONLINE, cpu=(22, 6), vols=[(40, "gp3", True)], imds="required", arch="arm64", ip="10.0.4.41"),
        dict(name="win-app-01", type="m5.large", os=WINDOWS, state="running", days=27, tags=_tags("win-app-01", "prod", "windows-ops", "B"), sys="ok", inst="ok", ssm=ONLINE, cpu=(52, 15), vols=[(100, "gp3", True)], imds="required", ip="10.0.5.51"),
        dict(name="win-app-02", type="m5.large", os=WINDOWS, state="running", days=27, tags=_tags("win-app-02", "prod", "windows-ops", "B"), sys="ok", inst="ok", ssm=NONE, cpu=(47, 13), vols=[(100, "gp3", True)], imds="required", ip="10.0.5.52"),
        dict(name="win-sql-01", type="r5.xlarge", os=WINDOWS, state="running", days=75, tags=_tags("win-sql-01", "prod", "dba-team", "C"), sys="impaired", inst="ok", ssm=LOST, cpu=(91, 5), vols=[(120, "gp3", True), (800, "io2", True)], imds="required", ip="10.0.5.61"),
        dict(name="win-file-01", type="m5.large", os=WINDOWS, state="running", days=130, tags=_tags("win-file-01", "prod", "windows-ops", "B"), sys="ok", inst="ok", ssm=ONLINE, cpu=(18, 7), vols=[(100, "gp3", True), (1000, "st1", True)], imds="required", ip="10.0.5.71"),
        dict(name="jump-host-01", type="t3.micro", os=LINUX, state="running", days=210, tags=_tags("jump-host-01", "shared"), sys="ok", inst="ok", ssm=ONLINE, cpu=(2, 1), vols=[(10, "gp2", False)], imds="optional", ip="10.0.9.91", public="3.91.44.12"),
        dict(name="batch-worker-01", type="c5.2xlarge", os=LINUX, state="running", days=5, tags=_tags("batch-worker-01", "staging", "data-team"), sys="ok", inst="ok", ssm=ONLINE, cpu=(88, 8), vols=[(60, "gp3", True)], imds="required", ip="10.0.6.81"),
        dict(name="build-agent-01", type="c5.large", os=LINUX, state="pending", days=0, tags=_tags("build-agent-01", "ci", "platform-team"), sys=None, inst=None, ssm=NONE, cpu=None, vols=[(30, "gp3", True)], imds="required", ip="10.0.7.99"),
        dict(name="dev-sandbox-01", type="t3.medium", os=LINUX, state="stopped", days=0, tags=_tags("dev-sandbox-01", "dev", "intern-team"), sys=None, inst=None, ssm=NONE, cpu=None, vols=[(30, "gp3", False)], imds="optional", ip="10.0.8.10"),
        dict(name="old-report-server", type="m4.large", os=WINDOWS, state="stopped", days=0, tags=_tags("old-report-server"), sys=None, inst=None, ssm=NONE, cpu=None, vols=[(100, "gp2", False)], imds="optional", ip="10.0.8.20"),
    ],
    "eu-west-1": [
        dict(name="eu-web-01", type="t3.medium", os=LINUX, state="running", days=33, tags=_tags("eu-web-01", "prod", "platform-team", "A"), sys="ok", inst="ok", ssm=ONLINE, cpu=(29, 9), vols=[(30, "gp3", True)], imds="required", ip="10.1.1.11", public="52.18.20.11"),
        dict(name="eu-db-01", type="r5.large", os=LINUX, state="running", days=33, tags=_tags("eu-db-01", "prod", "dba-team", "C"), sys="ok", inst="ok", ssm=ONLINE, cpu=(44, 10), vols=[(200, "gp3", True)], imds="required", ip="10.1.3.31"),
        dict(name="eu-win-01", type="m5.large", os=WINDOWS, state="running", days=33, tags=_tags("eu-win-01", "prod", "windows-ops", "B"), sys="ok", inst="ok", ssm=LOST, cpu=(36, 12), vols=[(100, "gp3", True)], imds="required", ip="10.1.5.51"),
    ],
}
# fmt: on


def _hex_id(prefix: str, number: int) -> str:
    return f"{prefix}-{format(0x0BADC0FFEE0000000 + number * 7919, '017x')}"


class DemoWorld:
    """Builds the fake fleet once; every fake client reads from it."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(timezone.utc)
        self.instances: dict[str, list[dict[str, Any]]] = {}
        self.specs: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        counter = 0
        for region, rows in FLEET.items():
            built = []
            for spec in rows:
                counter += 1
                instance = self._build_instance(region, spec, counter)
                self.specs[instance["InstanceId"]] = {**spec, "region": region}
                built.append(instance)
            self.instances[region] = built

    def _build_instance(self, region: str, spec: dict[str, Any], n: int) -> dict[str, Any]:
        az = f"{region}{'abc'[n % 3]}"
        windows = spec["os"] == WINDOWS
        running = spec["state"] == "running"
        launch = self.now - timedelta(days=spec["days"], hours=n % 7, minutes=(n * 13) % 60)
        if spec["state"] in {"pending", "stopped"}:
            launch = self.now - timedelta(days=60 + n) if spec["state"] == "stopped" else self.now - timedelta(seconds=40)

        mappings = []
        for index, (size, vtype, encrypted) in enumerate(spec["vols"]):
            volume_id = _hex_id("vol", n * 10 + index)
            device = "/dev/sda1" if index == 0 else f"xvd{'bcdef'[index - 1]}"
            self.volumes[volume_id] = {
                "VolumeId": volume_id,
                "Size": size,
                "VolumeType": vtype,
                "Iops": {"io2": 6000, "gp3": 3000, "gp2": size * 3}.get(vtype),
                "Encrypted": encrypted,
                "State": "in-use",
            }
            mappings.append(
                {"DeviceName": device, "Ebs": {"VolumeId": volume_id, "Status": "attached", "DeleteOnTermination": index == 0}}
            )

        instance: dict[str, Any] = {
            "InstanceId": _hex_id("i", n),
            "InstanceType": spec["type"],
            "ImageId": "ami-0windows2022demo0" if windows else ("ami-0al2023armdemo000" if spec.get("arch") == "arm64" else "ami-0al2023demo00000"),
            "State": {"Code": 16 if running else 0, "Name": spec["state"]},
            "Placement": {"AvailabilityZone": az},
            "LaunchTime": launch,
            "PrivateIpAddress": spec["ip"],
            "PrivateDnsName": f"ip-{spec['ip'].replace('.', '-')}.{region}.compute.internal",
            "Tags": [{"Key": k, "Value": v} for k, v in spec["tags"].items()],
            "BlockDeviceMappings": mappings,
            "MetadataOptions": {"HttpTokens": spec["imds"], "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 2},
            "Monitoring": {"State": "enabled" if n % 4 == 0 else "disabled"},
            "EbsOptimized": spec["type"][0] in "mrc",
            "Architecture": spec.get("arch", "x86_64"),
            "VirtualizationType": "hvm",
            "KeyName": None if windows else f"{region}-ops-key",
            "IamInstanceProfile": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:instance-profile/ssm-managed-instance"},
            "RootDeviceName": "/dev/sda1",
            "RootDeviceType": "ebs",
            "VpcId": f"vpc-0demo{region.replace('-', '')[:8]}",
            "SubnetId": f"subnet-0demo{az[-1]}000{n:02d}",
            "SourceDestCheck": True,
            "NetworkInterfaces": [{"NetworkInterfaceId": _hex_id("eni", n)}],
            "SecurityGroups": [
                {"GroupId": "sg-0demo000001", "GroupName": "default-ssh-rdp" if spec["tags"].get("Environment") == "shared" else "app-servers"},
            ],
            "Platform": "windows" if windows else None,
            "PlatformDetails": "Windows" if windows else "Linux/UNIX",
        }
        if not instance["KeyName"]:
            del instance["KeyName"]
        if not windows:
            del instance["Platform"]
        if spec.get("public"):
            instance["PublicIpAddress"] = spec["public"]
            instance["PublicDnsName"] = f"ec2-{spec['public'].replace('.', '-')}.compute-1.amazonaws.com"
        else:
            instance["PublicDnsName"] = ""
        return instance

    def region_instances(self, region: str) -> list[dict[str, Any]]:
        return self.instances.get(region, [])

    def status_for(self, instance: dict[str, Any]) -> dict[str, Any]:
        spec = self.specs[instance["InstanceId"]]
        state = spec["state"]
        item: dict[str, Any] = {
            "InstanceId": instance["InstanceId"],
            "AvailabilityZone": instance["Placement"]["AvailabilityZone"],
            "InstanceState": instance["State"],
        }
        if state == "running":
            item["SystemStatus"] = {"Status": spec["sys"]}
            item["InstanceStatus"] = {"Status": spec["inst"]}
            events = []
            if spec.get("event"):
                code, description, in_days = spec["event"]
                events.append({"Code": code, "Description": description, "NotBefore": self.now + timedelta(days=in_days)})
            # A finished event that the inspector must ignore.
            events.append({"Code": "instance-reboot", "Description": "[Completed] Scheduled reboot", "NotBefore": self.now - timedelta(days=20)})
            item["Events"] = events
        elif state == "pending":
            item["SystemStatus"] = {"Status": "initializing"}
            item["InstanceStatus"] = {"Status": "initializing"}
        else:
            item["SystemStatus"] = {"Status": "not-applicable"}
            item["InstanceStatus"] = {"Status": "not-applicable"}
        return item

    def ssm_item(self, instance: dict[str, Any]) -> dict[str, Any] | None:
        spec = self.specs[instance["InstanceId"]]
        if spec["state"] != "running" or spec["ssm"] is None:
            return None
        windows = spec["os"] == WINDOWS
        lost = spec["ssm"] == LOST
        return {
            "InstanceId": instance["InstanceId"],
            "PingStatus": spec["ssm"],
            "LastPingDateTime": self.now - (timedelta(days=3, hours=4) if lost else timedelta(seconds=45)),
            "AgentVersion": "3.3.1142.0",
            "PlatformName": "Microsoft Windows Server 2022 Datacenter" if windows else "Amazon Linux",
            "PlatformVersion": "10.0.20348" if windows else "2023",
        }

    def cpu_points(self, instance_id: str) -> list[dict[str, Any]]:
        spec = self.specs.get(instance_id)
        if not spec or spec["state"] != "running" or not spec.get("cpu"):
            return []
        average, swing = spec["cpu"]
        rng = random.Random(instance_id)
        top_of_hour = self.now.replace(minute=0, second=0, microsecond=0)
        points = []
        for hours_back in range(24, 0, -1):
            stamp = top_of_hour - timedelta(hours=hours_back)
            wave = math.sin(2 * math.pi * (stamp.hour - 9) / 24)
            value = min(99.0, max(0.5, average + swing * wave + rng.uniform(-2.5, 2.5)))
            points.append(
                {"Timestamp": stamp, "Average": round(value, 2), "Maximum": round(min(100.0, value + rng.uniform(3, 11)), 2), "Unit": "Percent"}
            )
        return points


# --------------------------------------------------------------------------- #
# Fake boto3-style clients (only the calls Ec2Inspector uses)
# --------------------------------------------------------------------------- #


def _not_found(instance_ids: list[str]) -> ClientError:
    message = f"The instance ID '{instance_ids[0]}' does not exist"
    return ClientError({"Error": {"Code": "InvalidInstanceID.NotFound", "Message": message}}, "DescribeInstances")


class _Paginator:
    def __init__(self, producer: Callable[..., Iterator[dict[str, Any]]]) -> None:
        self._producer = producer

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        return self._producer(**kwargs)


class DemoEc2:
    def __init__(self, world: DemoWorld, region: str) -> None:
        self.world = world
        self.region = region

    def get_paginator(self, name: str) -> _Paginator:
        producers = {
            "describe_instances": self._describe_instances,
            "describe_instance_status": self._describe_instance_status,
        }
        return _Paginator(producers[name])

    def _matching(self, instance_ids: list[str] | None, filters: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        instances = self.world.region_instances(self.region)
        if instance_ids:
            known = {i["InstanceId"] for i in instances}
            missing = [i for i in instance_ids if i not in known]
            if missing:
                raise _not_found(missing)
            instances = [i for i in instances if i["InstanceId"] in instance_ids]
        for flt in filters or []:
            name, values = flt["Name"], flt["Values"]
            if name == "instance-state-name":
                instances = [i for i in instances if i["State"]["Name"] in values]
            elif name.startswith("tag:"):
                key = name[4:]
                instances = [i for i in instances if any(t["Key"] == key and t["Value"] in values for t in i["Tags"])]
        return instances

    def _describe_instances(self, InstanceIds: list[str] | None = None, Filters: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        yield {"Reservations": [{"Instances": [i]} for i in self._matching(InstanceIds, Filters)]}

    def _describe_instance_status(self, InstanceIds: list[str] | None = None, IncludeAllInstances: bool = False) -> Iterator[dict[str, Any]]:
        instances = self._matching(InstanceIds, None)
        yield {"InstanceStatuses": [self.world.status_for(i) for i in instances]}

    def describe_volumes(self, VolumeIds: list[str]) -> dict[str, Any]:
        return {"Volumes": [self.world.volumes[v] for v in VolumeIds if v in self.world.volumes]}

    def describe_images(self, ImageIds: list[str]) -> dict[str, Any]:
        names = {
            "ami-0windows2022demo0": ("Windows_Server-2022-English-Full-Base-2026.10.15", "Microsoft Windows Server 2022 Full Locale English AMI"),
            "ami-0al2023demo00000": ("al2023-ami-2023.6.20261001.0-kernel-6.1-x86_64", "Amazon Linux 2023 AMI"),
            "ami-0al2023armdemo000": ("al2023-ami-2023.6.20261001.0-kernel-6.1-arm64", "Amazon Linux 2023 AMI (arm64)"),
        }
        images = []
        for image_id in ImageIds:
            if image_id in names:
                name, description = names[image_id]
                images.append({"ImageId": image_id, "Name": name, "Description": description, "CreationDate": "2026-10-15T08:30:00.000Z"})
        return {"Images": images}

    def describe_regions(self) -> dict[str, Any]:
        return {"Regions": [{"RegionName": name} for name in DEMO_REGIONS]}


class DemoSsm:
    def __init__(self, world: DemoWorld, region: str) -> None:
        self.world = world
        self.region = region

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_instance_information"
        return _Paginator(self._describe)

    def _describe(self, Filters: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        wanted: set[str] | None = None
        for flt in Filters or []:
            if flt["Key"] == "InstanceIds":
                wanted = set(flt["Values"])
        items = []
        for instance in self.world.region_instances(self.region):
            if wanted is not None and instance["InstanceId"] not in wanted:
                continue
            item = self.world.ssm_item(instance)
            if item:
                items.append(item)
        yield {"InstanceInformationList": items}


class DemoCloudWatch:
    def __init__(self, world: DemoWorld) -> None:
        self.world = world

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        instance_id = next(d["Value"] for d in kwargs["Dimensions"] if d["Name"] == "InstanceId")
        return {"Label": kwargs["MetricName"], "Datapoints": self.world.cpu_points(instance_id)}


def demo_client_factory(now: datetime | None = None) -> Callable[[str, str], Any]:
    """Return a ``client_factory(service, region)`` backed by the synthetic fleet."""
    world = DemoWorld(now)

    def factory(service: str, region: str) -> Any:
        if service == "ec2":
            return DemoEc2(world, region)
        if service == "ssm":
            return DemoSsm(world, region)
        if service == "cloudwatch":
            return DemoCloudWatch(world)
        raise ValueError(f"Demo mode does not simulate '{service}'.")

    return factory
