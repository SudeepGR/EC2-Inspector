"""
Offline tests for EC2 Inspector. No AWS account or network needed.

EC2 and CloudWatch are simulated with moto. moto does not implement the SSM
instance-information call, so SSM is replaced by a small fake client.

Run from the project root:
    pytest -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from fastapi.testclient import TestClient
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main as main_module  # noqa: E402
from app.inspector import (  # noqa: E402
    Ec2Inspector,
    InstanceNotFound,
    InvalidRequest,
    evaluate_health,
    parse_tag_filters,
)

REGION = "us-east-1"


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #


class FakeSsm:
    """Stands in for the SSM client's describe_instance_information paginator."""

    def __init__(self, items=None, error: Exception | None = None) -> None:
        self.items = items or []
        self.error = error

    def get_paginator(self, name: str):
        assert name == "describe_instance_information"
        outer = self

        class Paginator:
            def paginate(self, **kwargs):
                if outer.error:
                    raise outer.error
                items = outer.items
                for flt in kwargs.get("Filters", []):
                    if flt["Key"] == "InstanceIds":
                        items = [i for i in items if i["InstanceId"] in flt["Values"]]
                yield {"InstanceInformationList": items}

        return Paginator()


def ssm_item(instance_id: str, ping: str = "Online") -> dict:
    return {
        "InstanceId": instance_id,
        "PingStatus": ping,
        "LastPingDateTime": datetime.now(timezone.utc),
        "AgentVersion": "3.3.0",
        "PlatformName": "Amazon Linux",
        "PlatformVersion": "2023",
    }


def access_denied(operation: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, operation)


def make_factory(ssm: FakeSsm):
    def factory(service: str, region: str):
        if service == "ssm":
            return ssm
        return boto3.client(service, region_name=region)

    return factory


@pytest.fixture()
def aws():
    with mock_aws():
        yield


@pytest.fixture()
def fleet(aws):
    """Three instances: healthy web server, bare server without SSM, stopped box."""
    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]

    security_group = ec2.create_security_group(GroupName="web-sg", Description="test group")["GroupId"]

    def launch(tags: dict[str, str], groups: list[str] | None = None) -> str:
        response = ec2.run_instances(
            ImageId=ami,
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.micro",
            **({"SecurityGroupIds": groups} if groups else {}),
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
            ],
        )
        return response["Instances"][0]["InstanceId"]

    web = launch({"Name": "web-1", "Owner": "platform", "Environment": "prod"}, [security_group])
    bare = launch({"Name": "bare-1"})
    stopped = launch({"Name": "old-box", "Owner": "x", "Environment": "dev"})
    ec2.stop_instances(InstanceIds=[stopped])

    # CPU datapoints for the web server: three different hours, max 90.
    now = datetime.now(timezone.utc)
    cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    cloudwatch.put_metric_data(
        Namespace="AWS/EC2",
        MetricData=[
            {
                "MetricName": "CPUUtilization",
                "Dimensions": [{"Name": "InstanceId", "Value": web}],
                "Timestamp": now - timedelta(hours=hours),
                "Value": value,
                "Unit": "Percent",
            }
            for hours, value in ((5, 10.0), (3, 20.0), (1, 90.0))
        ],
    )

    ssm = FakeSsm([ssm_item(web)])  # bare and stopped are not registered
    inspector = Ec2Inspector(make_factory(ssm))
    return {"inspector": inspector, "web": web, "bare": bare, "stopped": stopped, "ssm": ssm}


# --------------------------------------------------------------------------- #
# Health rules (pure function)
# --------------------------------------------------------------------------- #

OK_STATUS = {"instance_status": "ok", "system_status": "ok", "events": []}
ONLINE = {"registered": True, "ping_status": "Online"}
ALL_TAGS = {"Name": "a", "Owner": "b", "Environment": "c"}
REQUIRED = ("Name", "Owner", "Environment")


def health(**overrides):
    args = dict(
        state="running",
        status=OK_STATUS,
        ssm=ONLINE,
        ssm_available=True,
        tags=ALL_TAGS,
        required_tags=REQUIRED,
        imds_tokens="required",
    )
    args.update(overrides)
    return evaluate_health(**args)


def codes(result):
    return {reason["code"] for reason in result["reasons"]}


def test_clean_running_instance_is_healthy():
    result = health()
    assert result["verdict"] == "HEALTHY"
    assert result["reasons"] == []


@pytest.mark.parametrize("key,code", [("system_status", "SYSTEM_STATUS_IMPAIRED"), ("instance_status", "INSTANCE_STATUS_IMPAIRED")])
def test_impaired_status_check_is_critical(key, code):
    result = health(status={**OK_STATUS, key: "impaired"})
    assert result["verdict"] == "CRITICAL"
    assert code in codes(result)


def test_scheduled_event_is_warning():
    event = {"code": "instance-reboot", "description": "Scheduled reboot", "not_before": "2026-12-01T00:00:00+00:00"}
    result = health(status={**OK_STATUS, "events": [event]})
    assert result["verdict"] == "WARNING"
    assert "(not before 2026-12-01 00:00 UTC)" in result["reasons"][0]["message"]


def test_event_time_formatting_handles_odd_input():
    from app.inspector import _readable_utc

    assert _readable_utc("2026-11-30T21:20:19.839365+00:00") == "2026-11-30 21:20 UTC"
    assert _readable_utc("2026-11-30T23:00:00+02:00") == "2026-11-30 21:00 UTC"  # converted to UTC
    assert _readable_utc("soon") == "soon"


def test_ssm_not_online_and_not_registered_are_warnings():
    lost = health(ssm={"registered": True, "ping_status": "ConnectionLost"})
    assert lost["verdict"] == "WARNING" and "SSM_NOT_ONLINE" in codes(lost)
    missing = health(ssm={"registered": False, "ping_status": None})
    assert missing["verdict"] == "WARNING" and "SSM_NOT_REGISTERED" in codes(missing)


def test_ssm_rules_skipped_when_ssm_data_unavailable():
    result = health(ssm={"registered": False, "ping_status": None}, ssm_available=False)
    assert result["verdict"] == "HEALTHY"


def test_critical_wins_over_warning():
    result = health(
        status={**OK_STATUS, "system_status": "impaired"},
        ssm={"registered": True, "ping_status": "ConnectionLost"},
    )
    assert result["verdict"] == "CRITICAL"
    assert [r["severity"] for r in result["reasons"]][:2] == ["CRITICAL", "WARNING"]


def test_new_instance_initializing_is_not_a_problem():
    result = health(status={"instance_status": "initializing", "system_status": "initializing", "events": []})
    assert result["verdict"] == "HEALTHY"
    assert {"INSTANCE_STATUS_INITIALIZING", "SYSTEM_STATUS_INITIALIZING"} <= codes(result)


def test_advisories_never_change_the_verdict():
    result = health(tags={"Name": "a"}, imds_tokens="optional")
    assert result["verdict"] == "HEALTHY"
    assert codes(result) == {"MISSING_TAGS", "IMDSV1_ENABLED"}
    assert "Owner, Environment" in result["reasons"][0]["message"]


@pytest.mark.parametrize(
    "state,verdict",
    [("stopped", "STOPPED"), ("stopping", "STOPPED"), ("pending", "PENDING"), ("terminated", "TERMINATED"), ("shutting-down", "TERMINATED")],
)
def test_non_running_states(state, verdict):
    # Even with broken SSM and impaired checks, a non-running instance is not "critical".
    result = health(
        state=state,
        status={**OK_STATUS, "system_status": "impaired"},
        ssm={"registered": False, "ping_status": None},
    )
    assert result["verdict"] == verdict
    assert not {"SYSTEM_STATUS_IMPAIRED", "SSM_NOT_REGISTERED"} & codes(result)


def test_completed_and_canceled_events_are_ignored():
    item = {
        "InstanceStatus": {"Status": "ok"},
        "SystemStatus": {"Status": "ok"},
        "Events": [
            {"Code": "instance-reboot", "Description": "[Completed] Scheduled reboot"},
            {"Code": "instance-retirement", "Description": "[Canceled] Retirement"},
            {"Code": "system-maintenance", "Description": "Upcoming maintenance"},
        ],
    }
    block = Ec2Inspector._status_block(item)
    assert [e["code"] for e in block["events"]] == ["system-maintenance"]


# --------------------------------------------------------------------------- #
# Collection against simulated AWS
# --------------------------------------------------------------------------- #


def test_list_instances_verdicts_totals_and_order(fleet):
    result = fleet["inspector"].list_instances(REGION)

    verdicts = {row["name"]: row["health"]["verdict"] for row in result["instances"]}
    assert verdicts == {"web-1": "HEALTHY", "bare-1": "WARNING", "old-box": "STOPPED"}
    assert result["totals"]["instances"] == 3
    assert result["totals"]["by_verdict"] == {"HEALTHY": 1, "WARNING": 1, "STOPPED": 1}
    assert result["data_sources"] == {"ec2": "ok", "ssm": "ok"}
    # Worst first: WARNING, then HEALTHY, then STOPPED.
    assert [row["name"] for row in result["instances"]] == ["bare-1", "web-1", "old-box"]


def test_list_row_content(fleet):
    rows = {r["name"]: r for r in fleet["inspector"].list_instances(REGION)["instances"]}
    web, bare, stopped = rows["web-1"], rows["bare-1"], rows["old-box"]

    assert web["instance_id"] == fleet["web"]
    assert web["instance_type"] == "t3.micro"
    assert web["tags"]["Owner"] == "platform"
    assert web["ssm_agent"]["ping_status"] == "Online"
    assert web["uptime_hours"] is not None and web["uptime_hours"] >= 0
    assert web["volume_count"] >= 1
    assert bare["ssm_agent"]["registered"] is False
    assert "SSM_NOT_REGISTERED" in codes(bare["health"])
    assert "MISSING_TAGS" in codes(bare["health"])
    assert stopped["uptime_hours"] is None


def test_detail_has_full_information(fleet):
    detail = fleet["inspector"].get_instance(REGION, fleet["web"])

    assert detail["instance_id"] == fleet["web"]
    assert detail["health"]["verdict"] == "HEALTHY"
    assert detail["status_checks"]["system_status"] == "ok"
    assert detail["ssm"]["agent_version"] == "3.3.0"
    assert detail["image"]["image_id"].startswith("ami-")
    assert detail["network"]["vpc_id"] and detail["network"]["subnet_id"]
    assert [g["name"] for g in detail["network"]["security_groups"]] == ["web-sg"]
    assert detail["storage"]["volumes"], "root volume should be listed"
    volume = detail["storage"]["volumes"][0]
    assert volume["volume_id"].startswith("vol-") and volume["size_gb"]
    assert detail["data_sources"] == {"ec2": "ok", "ssm": "ok", "cloudwatch": "ok"}
    assert "status" not in detail and "ssm_agent" not in detail  # renamed, not duplicated


def test_detail_cpu_statistics(fleet):
    cpu = fleet["inspector"].get_instance(REGION, fleet["web"])["performance"]["cpu_24h"]
    assert cpu["datapoints"] == 3
    assert cpu["maximum"] == 90.0
    assert cpu["average"] == pytest.approx(40.0, abs=0.1)
    assert [p["average"] for p in cpu["hourly"]] == [10.0, 20.0, 90.0]


def test_detail_without_cpu_data_is_not_an_error(fleet):
    detail = fleet["inspector"].get_instance(REGION, fleet["bare"])
    assert detail["performance"]["cpu_24h"]["datapoints"] == 0
    assert detail["performance"]["notes"] == []


def test_idle_and_high_cpu_notes():
    note = Ec2Inspector._performance
    assert "Low CPU" in note({"average": 2.0, "datapoints": 5}, "running")["notes"][0]
    assert "High CPU" in note({"average": 91.0, "datapoints": 5}, "running")["notes"][0]
    assert note({"average": 40.0, "datapoints": 5}, "running")["notes"] == []
    assert note({"average": 1.0, "datapoints": 5}, "stopped")["notes"] == []


def test_ssm_denied_degrades_gracefully(fleet):
    inspector = Ec2Inspector(make_factory(FakeSsm(error=access_denied())))
    result = inspector.list_instances(REGION)

    assert result["data_sources"]["ssm"] == "unavailable (AccessDeniedException)"
    by_name = {r["name"]: r for r in result["instances"]}
    assert by_name["bare-1"]["health"]["verdict"] == "HEALTHY"  # SSM rules skipped, not guessed
    assert by_name["bare-1"]["ssm_agent"]["ping_status"] == "unknown"


def test_detail_survives_cloudwatch_denied(fleet):
    class DenyCloudWatch:
        def get_metric_statistics(self, **_):
            raise access_denied("GetMetricStatistics")

    def factory(service, region):
        if service == "cloudwatch":
            return DenyCloudWatch()
        return make_factory(fleet["ssm"])(service, region)

    detail = Ec2Inspector(factory).get_instance(REGION, fleet["web"])
    assert detail["performance"]["cpu_24h"] is None
    assert detail["data_sources"]["cloudwatch"] == "unavailable (AccessDeniedException)"


def test_validation_errors(fleet):
    inspector = fleet["inspector"]
    with pytest.raises(InvalidRequest):
        inspector.list_instances("not a region")
    with pytest.raises(InvalidRequest):
        inspector.get_instance(REGION, "../etc/passwd")
    with pytest.raises(InstanceNotFound):
        inspector.get_instance(REGION, "i-0123456789abcdef0")


def test_required_tags_are_configurable(fleet):
    inspector = Ec2Inspector(make_factory(fleet["ssm"]), required_tags=("CostCenter",))
    rows = {r["name"]: r for r in inspector.list_instances(REGION)["instances"]}
    message = next(r["message"] for r in rows["web-1"]["health"]["reasons"] if r["code"] == "MISSING_TAGS")
    assert message.endswith("CostCenter")


def test_list_regions_falls_back_when_denied(aws):
    class Denied:
        def describe_regions(self):
            raise access_denied("DescribeRegions")

    inspector = Ec2Inspector(lambda service, region: Denied())
    assert inspector.list_regions("eu-west-1") == ["eu-west-1"]


def test_empty_region_returns_empty_list(aws):
    result = Ec2Inspector(make_factory(FakeSsm())).list_instances("eu-west-1")
    assert result["instances"] == [] and result["totals"]["instances"] == 0


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(fleet, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", REGION)
    main_module.app.dependency_overrides[main_module.get_inspector] = lambda: fleet["inspector"]
    yield TestClient(main_module.app)
    main_module.app.dependency_overrides.clear()


def test_healthz_and_dashboard(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "ec2-inspector"}
    page = client.get("/")
    assert page.status_code == 200 and "EC2 Inspector" in page.text


def test_dashboard_never_uses_innerhtml():
    # All dynamic values are inserted with textContent, so AWS tags cannot inject markup.
    assert "innerHTML" not in (ROOT / "app" / "static" / "index.html").read_text()


def test_api_instances_uses_default_region(client):
    body = client.get("/api/instances").json()
    assert body["region"] == REGION
    assert body["totals"]["instances"] == 3


def test_api_instance_detail(client, fleet):
    response = client.get(f"/api/instances/{fleet['web']}", params={"region": REGION})
    assert response.status_code == 200
    assert response.json()["health"]["verdict"] == "HEALTHY"


def test_api_error_mapping(client):
    assert client.get("/api/instances", params={"region": "bogus"}).status_code == 400
    assert client.get("/api/instances/not-an-id").status_code == 400
    assert client.get("/api/instances/i-0123456789abcdef0").status_code == 404


@pytest.mark.parametrize(
    "error,status",
    [(access_denied(), 403), (NoCredentialsError(), 503), (ClientError({"Error": {"Code": "Throttling"}}, "Op"), 502)],
)
def test_aws_errors_are_translated(error, status):
    class Broken:
        def list_instances(self, region, **filters):
            raise error

    main_module.app.dependency_overrides[main_module.get_inspector] = lambda: Broken()
    try:
        assert TestClient(main_module.app).get("/api/instances").status_code == status
    finally:
        main_module.app.dependency_overrides.clear()


def test_regions_endpoint(client):
    body = client.get("/api/regions").json()
    assert body["default"] == REGION and REGION in body["regions"]


def test_api_key_protects_api_but_not_health(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    assert client.get("/api/instances").status_code == 401
    assert client.get("/api/instances", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/api/instances", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def names(result):
    return [row["name"] for row in result["instances"]]


def test_filter_by_name_is_case_insensitive_substring(fleet):
    inspector = fleet["inspector"]
    assert names(inspector.list_instances(REGION, name="WEB")) == ["web-1"]
    assert names(inspector.list_instances(REGION, name="-1")) == ["bare-1", "web-1"]
    assert names(inspector.list_instances(REGION, name="nothing-matches")) == []
    assert names(inspector.list_instances(REGION, name="   ")) == names(inspector.list_instances(REGION))


def test_filter_by_state(fleet):
    inspector = fleet["inspector"]
    assert names(inspector.list_instances(REGION, states=["stopped"])) == ["old-box"]
    assert names(inspector.list_instances(REGION, states=["running"])) == ["bare-1", "web-1"]
    assert len(inspector.list_instances(REGION, states=["running", "stopped"])["instances"]) == 3


def test_filter_by_tag_uses_exact_match(fleet):
    inspector = fleet["inspector"]
    assert names(inspector.list_instances(REGION, tags={"Environment": "prod"})) == ["web-1"]
    assert names(inspector.list_instances(REGION, tags={"Environment": "pro"})) == []
    both = inspector.list_instances(REGION, tags={"Environment": "prod", "Owner": "platform"})
    assert names(both) == ["web-1"]
    assert names(inspector.list_instances(REGION, tags={"Environment": "prod", "Owner": "someone-else"})) == []


def test_filter_by_verdict(fleet):
    inspector = fleet["inspector"]
    assert names(inspector.list_instances(REGION, verdicts=["warning"])) == ["bare-1"]
    assert names(inspector.list_instances(REGION, verdicts=["WARNING", "STOPPED"])) == ["bare-1", "old-box"]
    assert names(inspector.list_instances(REGION, verdicts=["CRITICAL"])) == []


def test_filters_combine_with_and_and_totals_follow(fleet):
    result = fleet["inspector"].list_instances(REGION, name="1", states=["running"], verdicts=["HEALTHY"])
    assert names(result) == ["web-1"]
    assert result["totals"]["instances"] == 1
    assert result["totals"]["by_verdict"] == {"HEALTHY": 1}


def test_applied_filters_are_echoed(fleet):
    result = fleet["inspector"].list_instances(
        REGION, name=" web ", states=["stopped", "running"], tags={"Owner": "platform"}, verdicts=["warning"]
    )
    assert result["filters"] == {
        "name": "web",
        "states": ["running", "stopped"],
        "tags": {"Owner": "platform"},
        "verdicts": ["WARNING"],
    }
    assert fleet["inspector"].list_instances(REGION)["filters"] == {
        "name": None, "states": [], "tags": {}, "verdicts": []
    }


def test_invalid_filters_are_rejected(fleet):
    inspector = fleet["inspector"]
    with pytest.raises(InvalidRequest, match="Unknown state"):
        inspector.list_instances(REGION, states=["running", "sleeping"])
    with pytest.raises(InvalidRequest, match="Unknown verdict"):
        inspector.list_instances(REGION, verdicts=["BAD"])
    with pytest.raises(InvalidRequest, match="too long"):
        inspector.list_instances(REGION, name="x" * 300)
    with pytest.raises(InvalidRequest):
        inspector.list_instances(REGION, tags={f"k{i}": "v" for i in range(11)})


def test_parse_tag_filters():
    assert parse_tag_filters(None) == {}
    assert parse_tag_filters(["Environment=prod", " Owner = a=b "]) == {"Environment": "prod", "Owner": "a=b"}
    for bad in (["novalue"], ["=value"], ["key="], ["a=1", "a=2"]):
        with pytest.raises(InvalidRequest):
            parse_tag_filters(bad)
    with pytest.raises(InvalidRequest, match="At most"):
        parse_tag_filters([f"k{i}=v" for i in range(11)])


def test_api_filters(client):
    def get(**params):
        return client.get("/api/instances", params=params)

    assert [r["name"] for r in get(name="web").json()["instances"]] == ["web-1"]
    assert [r["name"] for r in get(state="stopped").json()["instances"]] == ["old-box"]
    assert [r["name"] for r in get(tag="Environment=prod").json()["instances"]] == ["web-1"]
    assert [r["name"] for r in get(verdict="WARNING").json()["instances"]] == ["bare-1"]

    several = client.get("/api/instances?state=running&state=stopped&verdict=WARNING&verdict=STOPPED")
    assert [r["name"] for r in several.json()["instances"]] == ["bare-1", "old-box"]

    combined = client.get("/api/instances?name=web&state=running&tag=Environment=prod&verdict=HEALTHY")
    assert [r["name"] for r in combined.json()["instances"]] == ["web-1"]


def test_api_rejects_bad_filters(client):
    assert client.get("/api/instances?state=bogus").status_code == 400
    assert client.get("/api/instances?tag=missing-equals").status_code == 400
    assert client.get("/api/instances?verdict=NOPE").status_code == 400
    assert client.get("/api/instances?tag=a=1&tag=a=2").status_code == 400
