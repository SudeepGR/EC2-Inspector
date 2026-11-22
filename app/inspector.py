"""
EC2 inspection and health evaluation.

``Ec2Inspector`` reads instance details from EC2, SSM and CloudWatch and returns
JSON-ready dictionaries. ``evaluate_health`` is a pure function that turns the
collected facts into a verdict (HEALTHY, WARNING, CRITICAL, STOPPED, ...) with a
list of reasons, so the rules can be tested without any AWS calls.

Everything here is READ-ONLY: no instance is started, stopped or modified.

Verdict rules use only data available in both the list and the detail views
(state, EC2 status checks, scheduled events, SSM agent status), so an instance
never shows one verdict in the table and another in its detail panel. CPU is
reported as a performance note and does not change the verdict.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Called as client_factory("ec2", "us-east-1") and returns a boto3-style client.
ClientFactory = Callable[[str, str], Any]

INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

DEFAULT_REQUIRED_TAGS = ("Name", "Owner", "Environment")
CPU_HIGH_PERCENT = 85.0
CPU_IDLE_PERCENT = 5.0

SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}

VALID_STATES = {"pending", "running", "stopping", "stopped", "shutting-down", "terminated"}
VALID_VERDICTS = {"CRITICAL", "WARNING", "HEALTHY", "PENDING", "STOPPED", "TERMINATED"}
MAX_TAG_FILTERS = 10
MAX_NAME_FILTER_LENGTH = 256


class InvalidRequest(ValueError):
    """The caller sent a malformed region or instance id."""


class InstanceNotFound(LookupError):
    """The instance id does not exist in the region."""


def parse_tag_filters(items: list[str] | None) -> dict[str, str]:
    """Turn ["Environment=prod", "Owner=x"] into {"Environment": "prod", "Owner": "x"}."""
    tags: dict[str, str] = {}
    for item in items or []:
        key, separator, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or not value:
            raise InvalidRequest(f"Tag filter '{item}' must look like Key=Value.")
        if len(key) > 128 or len(value) > 256:
            raise InvalidRequest("Tag key or value is too long.")
        if key in tags:
            raise InvalidRequest(f"Tag key '{key}' was given more than once.")
        tags[key] = value
    if len(tags) > MAX_TAG_FILTERS:
        raise InvalidRequest(f"At most {MAX_TAG_FILTERS} tag filters are allowed.")
    return tags


# --------------------------------------------------------------------------- #
# Health rules (pure, no AWS calls)
# --------------------------------------------------------------------------- #


def evaluate_health(
    *,
    state: str,
    status: dict[str, Any],
    ssm: dict[str, Any],
    ssm_available: bool,
    tags: dict[str, str],
    required_tags: tuple[str, ...] | list[str],
    imds_tokens: str | None,
) -> dict[str, Any]:
    """Return ``{"verdict": ..., "reasons": [...]}`` for one instance."""
    reasons: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        reasons.append({"severity": severity, "code": code, "message": message})

    if state in {"terminated", "shutting-down"}:
        return {"verdict": "TERMINATED", "reasons": reasons}

    if state in {"stopped", "stopping"}:
        add("INFO", "INSTANCE_STOPPED", f"Instance is {state}; no health checks apply.")
        verdict = "STOPPED"
    elif state == "pending":
        add("INFO", "INSTANCE_PENDING", "Instance is starting up.")
        verdict = "PENDING"
    else:
        verdict = "RUNNING"  # refined below
        _running_checks(add, status, ssm, ssm_available)

    # Advisories: shown to the user but they never change the verdict.
    missing_tags = [tag for tag in required_tags if not tags.get(tag)]
    if missing_tags:
        add("INFO", "MISSING_TAGS", "Missing required tags: " + ", ".join(missing_tags))
    if imds_tokens == "optional":
        add("INFO", "IMDSV1_ENABLED", "IMDSv1 is allowed (HttpTokens=optional); IMDSv2 is recommended.")

    if verdict == "RUNNING":
        severities = {reason["severity"] for reason in reasons}
        if "CRITICAL" in severities:
            verdict = "CRITICAL"
        elif "WARNING" in severities:
            verdict = "WARNING"
        else:
            verdict = "HEALTHY"

    reasons.sort(key=lambda reason: SEVERITY_ORDER[reason["severity"]])
    return {"verdict": verdict, "reasons": reasons}


def _readable_utc(iso: str) -> str:
    """'2026-11-30T21:20:19.8+00:00' -> '2026-11-30 21:20 UTC' (falls back to the raw text)."""
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def _running_checks(add, status: dict[str, Any], ssm: dict[str, Any], ssm_available: bool) -> None:
    checks = (
        ("system_status", "SYSTEM_STATUS", "AWS system status check", "underlying host or network problem"),
        ("instance_status", "INSTANCE_STATUS", "Instance status check", "operating system or guest network problem"),
    )
    for key, code, label, meaning in checks:
        value = status.get(key)
        if value == "impaired":
            add("CRITICAL", f"{code}_IMPAIRED", f"{label} is impaired ({meaning}).")
        elif value in {"initializing", "insufficient-data"}:
            add("INFO", f"{code}_{value.upper().replace('-', '_')}", f"{label}: {value} (normal shortly after launch).")
        elif value is None:
            add("INFO", f"{code}_NOT_REPORTED", f"{label} has not been reported yet.")

    for event in status.get("events", []):
        when = f" (not before {_readable_utc(event['not_before'])})" if event.get("not_before") else ""
        add("WARNING", "SCHEDULED_EVENT", f"{event['code']}: {event['description']}{when}")

    if ssm_available:
        if not ssm.get("registered"):
            add("WARNING", "SSM_NOT_REGISTERED", "Instance is not registered with SSM (agent missing, role missing, or no network path).")
        elif ssm.get("ping_status") != "Online":
            add("WARNING", "SSM_NOT_ONLINE", f"SSM agent status is {ssm.get('ping_status')}.")


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #


class Ec2Inspector:
    """Collects EC2 details and health data for one region at a time."""

    def __init__(
        self,
        client_factory: ClientFactory,
        required_tags: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_TAGS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client_factory = client_factory
        self.required_tags = tuple(required_tags)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------ public -------------------------------- #

    def list_regions(self, default_region: str) -> list[str]:
        """Return enabled regions; fall back to the default if not permitted."""
        try:
            response = self.client_factory("ec2", default_region).describe_regions()
            return sorted(item["RegionName"] for item in response["Regions"])
        except ClientError as exc:
            logger.warning("describe_regions failed: %s", exc)
            return [default_region]

    def list_instances(
        self,
        region: str,
        *,
        name: str | None = None,
        states: list[str] | None = None,
        tags: dict[str, str] | None = None,
        verdicts: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Summary and health verdict for the instances in the region.

        Optional filters (all combined with AND):
            name      case-insensitive substring of the Name tag
            states    instance states, e.g. ["running", "stopped"]
            tags      exact Key=Value matches, e.g. {"Environment": "prod"}
            verdicts  health verdicts, e.g. ["WARNING", "CRITICAL"]
        """
        self._check_region(region)
        ec2_filters, name_needle, verdict_set = self._build_filters(name, states, tags, verdicts)
        now = self.clock()
        ec2 = self.client_factory("ec2", region)

        instances = self._fetch_instances(ec2, filters=ec2_filters)
        if name_needle:
            instances = [
                inst
                for inst in instances
                if name_needle in next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "").lower()
            ]
        status_map = self._fetch_status_map(ec2)
        ssm_map, ssm_source = self._fetch_ssm_map(region)

        rows = [
            self._summary(inst, status_map.get(inst["InstanceId"]), ssm_map, now)
            for inst in instances
        ]
        if verdict_set:
            rows = [row for row in rows if row["health"]["verdict"] in verdict_set]
        rows.sort(key=lambda row: (_verdict_rank(row["health"]["verdict"]), row["name"] or "", row["instance_id"]))

        return {
            "region": region,
            "generated_at": now.isoformat(),
            "filters": {
                "name": name.strip() if name and name.strip() else None,
                "states": sorted(states) if states else [],
                "tags": tags or {},
                "verdicts": sorted(verdict_set),
            },
            "data_sources": {"ec2": "ok", "ssm": ssm_source},
            "totals": self._totals(rows),
            "instances": rows,
        }

    def get_instance(self, region: str, instance_id: str) -> dict[str, Any]:
        """Full details, health verdict and recent CPU for one instance."""
        self._check_region(region)
        if not INSTANCE_ID_RE.match(instance_id or ""):
            raise InvalidRequest("Instance id must look like i-0123456789abcdef0.")

        now = self.clock()
        ec2 = self.client_factory("ec2", region)

        instances = self._fetch_instances(ec2, [instance_id])
        if not instances:
            raise InstanceNotFound(f"Instance {instance_id} not found in {region}.")
        inst = instances[0]

        status_map = self._fetch_status_map(ec2, [instance_id])
        ssm_map, ssm_source = self._fetch_ssm_map(region, [instance_id])
        summary = self._summary(inst, status_map.get(instance_id), ssm_map, now)

        volumes = self._fetch_volumes(ec2, inst)
        cpu, cw_source = self._fetch_cpu(region, instance_id, now)

        summary.update(
            {
                "generated_at": now.isoformat(),
                "data_sources": {"ec2": "ok", "ssm": ssm_source, "cloudwatch": cw_source},
                "status_checks": summary.pop("status"),
                "ssm": summary.pop("ssm_agent"),
                "image": self._image_info(ec2, inst.get("ImageId")),
                "architecture": inst.get("Architecture"),
                "virtualization_type": inst.get("VirtualizationType"),
                "key_name": inst.get("KeyName"),
                "iam_instance_profile": (inst.get("IamInstanceProfile") or {}).get("Arn"),
                "ebs_optimized": inst.get("EbsOptimized"),
                "monitoring": (inst.get("Monitoring") or {}).get("State"),
                "metadata_options": {
                    "http_tokens": (inst.get("MetadataOptions") or {}).get("HttpTokens"),
                    "http_endpoint": (inst.get("MetadataOptions") or {}).get("HttpEndpoint"),
                    "hop_limit": (inst.get("MetadataOptions") or {}).get("HttpPutResponseHopLimit"),
                },
                "network": {
                    "vpc_id": inst.get("VpcId"),
                    "subnet_id": inst.get("SubnetId"),
                    "private_dns_name": inst.get("PrivateDnsName") or None,
                    "public_dns_name": inst.get("PublicDnsName") or None,
                    "source_dest_check": inst.get("SourceDestCheck"),
                    "network_interfaces": len(inst.get("NetworkInterfaces", [])),
                    "security_groups": [
                        {"id": sg.get("GroupId"), "name": sg.get("GroupName")}
                        for sg in inst.get("SecurityGroups", [])
                    ],
                },
                "storage": {
                    "root_device_name": inst.get("RootDeviceName"),
                    "root_device_type": inst.get("RootDeviceType"),
                    "volumes": volumes,
                },
                "performance": self._performance(cpu, summary["state"]),
            }
        )
        return summary

    # --------------------------- AWS fetchers ----------------------------- #

    @staticmethod
    def _fetch_instances(
        ec2: Any,
        instance_ids: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"InstanceIds": instance_ids} if instance_ids else {}
        if filters:
            kwargs["Filters"] = filters
        found: list[dict[str, Any]] = []
        try:
            for page in ec2.get_paginator("describe_instances").paginate(**kwargs):
                for reservation in page["Reservations"]:
                    found.extend(reservation["Instances"])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "").startswith("InvalidInstanceID"):
                return []
            raise
        return found

    @staticmethod
    def _fetch_status_map(ec2: Any, instance_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        kwargs: dict[str, Any] = {"IncludeAllInstances": True}
        if instance_ids:
            kwargs["InstanceIds"] = instance_ids
        result: dict[str, dict[str, Any]] = {}
        try:
            for page in ec2.get_paginator("describe_instance_status").paginate(**kwargs):
                for item in page["InstanceStatuses"]:
                    result[item["InstanceId"]] = item
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "").startswith("InvalidInstanceID"):
                return {}
            raise
        return result

    def _fetch_ssm_map(
        self, region: str, instance_ids: list[str] | None = None
    ) -> tuple[dict[str, dict[str, Any]] | None, str]:
        """Return (map, source_status). map is None when SSM cannot be read."""
        kwargs: dict[str, Any] = {}
        if instance_ids:
            kwargs["Filters"] = [{"Key": "InstanceIds", "Values": instance_ids}]
        try:
            ssm = self.client_factory("ssm", region)
            result: dict[str, dict[str, Any]] = {}
            for page in ssm.get_paginator("describe_instance_information").paginate(**kwargs):
                for item in page["InstanceInformationList"]:
                    result[item["InstanceId"]] = item
            return result, "ok"
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Error")
            logger.warning("SSM unavailable: %s", code)
            return None, f"unavailable ({code})"

    @staticmethod
    def _fetch_volumes(ec2: Any, inst: dict[str, Any]) -> list[dict[str, Any]]:
        mappings = {
            m["Ebs"]["VolumeId"]: m
            for m in inst.get("BlockDeviceMappings", [])
            if m.get("Ebs", {}).get("VolumeId")
        }
        if not mappings:
            return []
        details: dict[str, dict[str, Any]] = {}
        try:
            response = ec2.describe_volumes(VolumeIds=list(mappings))
            details = {v["VolumeId"]: v for v in response["Volumes"]}
        except ClientError as exc:
            logger.warning("describe_volumes failed: %s", exc)

        rows = []
        for volume_id, mapping in mappings.items():
            volume = details.get(volume_id, {})
            rows.append(
                {
                    "volume_id": volume_id,
                    "device": mapping.get("DeviceName"),
                    "attach_status": mapping["Ebs"].get("Status"),
                    "delete_on_termination": mapping["Ebs"].get("DeleteOnTermination"),
                    "size_gb": volume.get("Size"),
                    "type": volume.get("VolumeType"),
                    "iops": volume.get("Iops"),
                    "encrypted": volume.get("Encrypted"),
                    "state": volume.get("State"),
                }
            )
        return rows

    @staticmethod
    def _image_info(ec2: Any, image_id: str | None) -> dict[str, Any]:
        info: dict[str, Any] = {"image_id": image_id, "name": None, "description": None, "created": None}
        if not image_id:
            return info
        try:
            images = ec2.describe_images(ImageIds=[image_id])["Images"]
        except ClientError:
            return info  # AMI may be deregistered or private
        if images:
            info.update(
                name=images[0].get("Name"),
                description=images[0].get("Description"),
                created=images[0].get("CreationDate"),
            )
        return info

    def _fetch_cpu(self, region: str, instance_id: str, now: datetime) -> tuple[dict[str, Any] | None, str]:
        try:
            cloudwatch = self.client_factory("cloudwatch", region)
            response = cloudwatch.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=now - timedelta(hours=24),
                EndTime=now,
                Period=3600,
                Statistics=["Average", "Maximum"],
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Error")
            logger.warning("CloudWatch unavailable: %s", code)
            return None, f"unavailable ({code})"

        points = sorted(response.get("Datapoints", []), key=lambda p: p["Timestamp"])
        if not points:
            return {"datapoints": 0, "average": None, "maximum": None, "hourly": []}, "ok"
        return (
            {
                "datapoints": len(points),
                "average": round(sum(p["Average"] for p in points) / len(points), 1),
                "maximum": round(max(p["Maximum"] for p in points), 1),
                "hourly": [
                    {"time": p["Timestamp"].isoformat(), "average": round(p["Average"], 1)}
                    for p in points
                ],
            },
            "ok",
        )

    # --------------------------- shaping helpers -------------------------- #

    def _summary(
        self,
        inst: dict[str, Any],
        status_item: dict[str, Any] | None,
        ssm_map: dict[str, dict[str, Any]] | None,
        now: datetime,
    ) -> dict[str, Any]:
        state = inst["State"]["Name"]
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        status = self._status_block(status_item)
        ssm_available = ssm_map is not None
        ssm_block = self._ssm_block((ssm_map or {}).get(inst["InstanceId"]))

        health = evaluate_health(
            state=state,
            status=status,
            ssm=ssm_block,
            ssm_available=ssm_available,
            tags=tags,
            required_tags=self.required_tags,
            imds_tokens=(inst.get("MetadataOptions") or {}).get("HttpTokens"),
        )

        launch_time = inst.get("LaunchTime")
        uptime_hours = None
        if state == "running" and launch_time:
            uptime_hours = round((now - launch_time).total_seconds() / 3600, 1)

        platform_text = inst.get("PlatformDetails") or inst.get("Platform") or "Linux/UNIX"
        return {
            "instance_id": inst["InstanceId"],
            "name": tags.get("Name"),
            "state": state,
            "instance_type": inst.get("InstanceType"),
            "availability_zone": (inst.get("Placement") or {}).get("AvailabilityZone"),
            "platform": "Windows" if "windows" in str(platform_text).lower() else "Linux",
            "private_ip": inst.get("PrivateIpAddress"),
            "public_ip": inst.get("PublicIpAddress"),
            "launch_time": launch_time.isoformat() if launch_time else None,
            "uptime_hours": uptime_hours,
            "volume_count": len([m for m in inst.get("BlockDeviceMappings", []) if m.get("Ebs")]),
            "tags": tags,
            "health": health,
            "status": status,
            "ssm_agent": ssm_block if ssm_available else {"registered": None, "ping_status": "unknown"},
        }

    @staticmethod
    def _status_block(item: dict[str, Any] | None) -> dict[str, Any]:
        if not item:
            return {"instance_status": None, "system_status": None, "events": []}
        events = [
            {
                "code": event.get("Code"),
                "description": event.get("Description", ""),
                "not_before": event["NotBefore"].isoformat() if event.get("NotBefore") else None,
            }
            for event in item.get("Events", [])
            # AWS prefixes finished events with "[Completed]" or "[Canceled]".
            if not str(event.get("Description", "")).startswith(("[Completed]", "[Canceled]"))
        ]
        return {
            "instance_status": (item.get("InstanceStatus") or {}).get("Status"),
            "system_status": (item.get("SystemStatus") or {}).get("Status"),
            "events": events,
        }

    @staticmethod
    def _ssm_block(item: dict[str, Any] | None) -> dict[str, Any]:
        if not item:
            return {"registered": False, "ping_status": None}
        platform = " ".join(
            part for part in (item.get("PlatformName"), item.get("PlatformVersion")) if part
        )
        last_ping = item.get("LastPingDateTime")
        return {
            "registered": True,
            "ping_status": item.get("PingStatus"),
            "last_ping": last_ping.isoformat() if last_ping else None,
            "agent_version": item.get("AgentVersion"),
            "platform": platform or None,
        }

    @staticmethod
    def _performance(cpu: dict[str, Any] | None, state: str) -> dict[str, Any]:
        notes: list[str] = []
        if cpu and cpu["average"] is not None and state == "running":
            if cpu["average"] >= CPU_HIGH_PERCENT:
                notes.append(f"High CPU: 24h average {cpu['average']}% (threshold {CPU_HIGH_PERCENT}%).")
            elif cpu["average"] < CPU_IDLE_PERCENT:
                notes.append(
                    f"Low CPU: 24h average {cpu['average']}% (possible idle instance; "
                    "check a longer period before resizing)."
                )
        return {"cpu_24h": cpu, "notes": notes}

    @staticmethod
    def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_verdict: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for row in rows:
            by_verdict[row["health"]["verdict"]] = by_verdict.get(row["health"]["verdict"], 0) + 1
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        return {"instances": len(rows), "by_verdict": by_verdict, "by_state": by_state}

    @staticmethod
    def _build_filters(
        name: str | None,
        states: list[str] | None,
        tags: dict[str, str] | None,
        verdicts: list[str] | None,
    ) -> tuple[list[dict[str, Any]], str | None, set[str]]:
        """Validate filters; return (EC2 API filters, name needle, verdict set)."""
        ec2_filters: list[dict[str, Any]] = []

        if states:
            bad = sorted(set(states) - VALID_STATES)
            if bad:
                raise InvalidRequest(f"Unknown state(s): {', '.join(bad)}. Use: {', '.join(sorted(VALID_STATES))}.")
            ec2_filters.append({"Name": "instance-state-name", "Values": sorted(set(states))})

        if tags:
            if len(tags) > MAX_TAG_FILTERS:
                raise InvalidRequest(f"At most {MAX_TAG_FILTERS} tag filters are allowed.")
            for key, value in tags.items():
                ec2_filters.append({"Name": f"tag:{key}", "Values": [value]})

        needle = None
        if name and name.strip():
            if len(name) > MAX_NAME_FILTER_LENGTH:
                raise InvalidRequest("Name filter is too long.")
            needle = name.strip().lower()

        verdict_set = {v.upper() for v in verdicts or []}
        bad_verdicts = sorted(verdict_set - VALID_VERDICTS)
        if bad_verdicts:
            raise InvalidRequest(f"Unknown verdict(s): {', '.join(bad_verdicts)}.")

        return ec2_filters, needle, verdict_set

    @staticmethod
    def _check_region(region: str) -> None:
        if not REGION_RE.match(region or ""):
            raise InvalidRequest("Region must look like us-east-1.")


def _verdict_rank(verdict: str) -> int:
    order = ["CRITICAL", "WARNING", "PENDING", "HEALTHY", "STOPPED", "TERMINATED"]
    return order.index(verdict) if verdict in order else len(order)
