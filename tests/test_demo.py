"""Tests for DEMO_MODE: the built-in synthetic fleet must behave like real AWS data."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main as main_module  # noqa: E402
from app.demo import DEMO_REGIONS, demo_client_factory  # noqa: E402
from app.inspector import Ec2Inspector  # noqa: E402


@pytest.fixture(scope="module")
def inspector():
    return Ec2Inspector(demo_client_factory())


@pytest.fixture(scope="module")
def fleet(inspector):
    return inspector.list_instances("us-east-1")


def by_name(result):
    return {row["name"]: row for row in result["instances"]}


def test_demo_fleet_shows_every_verdict(fleet):
    assert fleet["totals"]["instances"] == 16
    assert fleet["totals"]["by_verdict"] == {
        "CRITICAL": 2, "WARNING": 3, "HEALTHY": 8, "STOPPED": 2, "PENDING": 1,
    }
    assert fleet["data_sources"] == {"ec2": "ok", "ssm": "ok"}


def test_demo_verdicts_match_the_story(fleet):
    rows = by_name(fleet)
    expected = {
        "api-prod-01": "CRITICAL",   # instance status impaired
        "win-sql-01": "CRITICAL",    # system status impaired
        "web-prod-03": "WARNING",    # SSM ConnectionLost
        "db-prod-02": "WARNING",     # scheduled retirement
        "win-app-02": "WARNING",     # SSM not registered
        "web-prod-01": "HEALTHY",
        "jump-host-01": "HEALTHY",   # advisories only
        "dev-sandbox-01": "STOPPED",
        "build-agent-01": "PENDING",
    }
    assert {name: rows[name]["health"]["verdict"] for name in expected} == expected


def test_completed_events_are_ignored_but_pending_ones_are_shown(inspector, fleet):
    detail = inspector.get_instance("us-east-1", by_name(fleet)["db-prod-02"]["instance_id"])
    events = detail["status_checks"]["events"]
    assert [e["code"] for e in events] == ["instance-retirement"]


def test_every_demo_instance_has_a_working_detail_view(inspector):
    for region in DEMO_REGIONS:
        for row in inspector.list_instances(region)["instances"]:
            detail = inspector.get_instance(region, row["instance_id"])
            assert detail["health"]["verdict"] == row["health"]["verdict"]
            assert detail["data_sources"] == {"ec2": "ok", "ssm": "ok", "cloudwatch": "ok"}


def test_demo_detail_content(inspector, fleet):
    rows = by_name(fleet)
    sql = inspector.get_instance("us-east-1", rows["win-sql-01"]["instance_id"])
    assert sql["platform"] == "Windows"
    assert sql["performance"]["cpu_24h"]["datapoints"] == 24
    assert sql["performance"]["cpu_24h"]["average"] > 85
    assert "High CPU" in sql["performance"]["notes"][0]
    assert [v["type"] for v in sql["storage"]["volumes"]] == ["gp3", "io2"]

    jump = inspector.get_instance("us-east-1", rows["jump-host-01"]["instance_id"])
    assert "Low CPU" in jump["performance"]["notes"][0]
    assert {"MISSING_TAGS", "IMDSV1_ENABLED"} <= {r["code"] for r in jump["health"]["reasons"]}
    assert jump["storage"]["volumes"][0]["encrypted"] is False

    stopped = inspector.get_instance("us-east-1", rows["dev-sandbox-01"]["instance_id"])
    assert stopped["performance"]["cpu_24h"]["datapoints"] == 0


def test_demo_filters_work(inspector):
    windows_names = {r["name"] for r in inspector.list_instances("us-east-1", tags={"Owner": "windows-ops"})["instances"]}
    assert windows_names == {"win-app-01", "win-app-02", "win-file-01"}
    stopped = inspector.list_instances("us-east-1", states=["stopped"])
    assert {r["name"] for r in stopped["instances"]} == {"dev-sandbox-01", "old-report-server"}
    warnings = inspector.list_instances("us-east-1", verdicts=["WARNING", "CRITICAL"])
    assert warnings["totals"]["instances"] == 5


def test_other_demo_regions(inspector):
    assert inspector.list_regions("us-east-1") == sorted(DEMO_REGIONS)
    assert inspector.list_instances("eu-west-1")["totals"]["instances"] == 3
    assert inspector.list_instances("ap-south-1")["instances"] == []


def test_demo_data_is_synthetic():
    # Guard: demo IDs and the account id must be recognisably fake.
    result = Ec2Inspector(demo_client_factory()).list_instances("us-east-1")
    assert all(row["instance_id"].startswith("i-0badc0ffee") for row in result["instances"])


def test_demo_mode_through_the_api(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    main_module._demo_factory.cache_clear()
    client = TestClient(main_module.app)

    regions = client.get("/api/regions").json()
    assert regions["demo"] is True and regions["default"] == "us-east-1"
    assert client.get("/api/instances").json()["totals"]["instances"] == 16

    row = client.get("/api/instances?verdict=CRITICAL").json()["instances"][0]
    detail = client.get(f"/api/instances/{row['instance_id']}")
    assert detail.status_code == 200
    assert client.get("/api/instances/i-0123456789abcdef0").status_code == 404

    monkeypatch.setenv("DEMO_MODE", "0")
    assert main_module.demo_mode() is False
