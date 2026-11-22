"""
FastAPI backend for EC2 Inspector.

Endpoints (all read-only):
    GET /                       the dashboard page
    GET /healthz                liveness probe (no AWS calls)
    GET /api/regions            enabled regions
    GET /api/instances          instances in a region with a health verdict
                                optional filters: name, state, tag, verdict
    GET /api/instances/{id}     full details for one instance

Configuration (environment variables):
    DEMO_MODE       1 = use a built-in fake fleet, no AWS account needed
    AWS_REGION      default region for the dashboard (default us-east-1)
    REQUIRED_TAGS   comma-separated tags every instance should have
                    (default Name,Owner,Environment)
    API_KEY         if set, /api/* requires an X-API-Key header

AWS credentials come from boto3's normal chain (CLI profile locally, task or
instance role when deployed). See iam-policy.json for the permissions needed.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
)
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse

from app.demo import demo_client_factory
from app.inspector import (
    DEFAULT_REQUIRED_TAGS,
    Ec2Inspector,
    InstanceNotFound,
    InvalidRequest,
    parse_tag_filters,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
AUTH_ERROR_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthFailure"}

app = FastAPI(
    title="EC2 Inspector",
    version="1.0.0",
    description="Read-only EC2 instance details and health verdicts",
)


def default_region() -> str:
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"


@lru_cache(maxsize=1)
def _session() -> boto3.session.Session:
    return boto3.session.Session()


def _client_factory(service: str, region: str) -> Any:
    config = Config(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=20,
    )
    return _session().client(service, region_name=region, config=config)


def demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def _demo_factory():
    return demo_client_factory()


def get_inspector() -> Ec2Inspector:
    """Dependency; tests replace it with an inspector backed by fake AWS."""
    tags = [t.strip() for t in os.getenv("REQUIRED_TAGS", "").split(",") if t.strip()]
    factory = _demo_factory() if demo_mode() else _client_factory
    return Ec2Inspector(factory, required_tags=tags or DEFAULT_REQUIRED_TAGS)


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """Optional shared-secret check. Disabled unless API_KEY is set."""
    expected = os.getenv("API_KEY")
    if not expected:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")


def _translate_errors(exc: Exception) -> HTTPException:
    """Map inspector and AWS errors to clear HTTP responses."""
    if isinstance(exc, InvalidRequest):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, InstanceNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, NoCredentialsError):
        return HTTPException(status_code=503, detail="No AWS credentials found. Configure a profile or role.")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Error")
        if code in AUTH_ERROR_CODES:
            return HTTPException(status_code=403, detail=f"AWS denied the request ({code}). Check iam-policy.json.")
        return HTTPException(status_code=502, detail=f"AWS request failed ({code}).")
    if isinstance(exc, BotoCoreError):
        return HTTPException(status_code=502, detail=f"Could not reach AWS: {exc}")
    raise exc


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "ec2-inspector"}


@app.get("/api/regions", dependencies=[Depends(require_api_key)])
def regions(inspector: Ec2Inspector = Depends(get_inspector)) -> dict[str, Any]:
    default = default_region()
    try:
        return {
            "default": default,
            "regions": inspector.list_regions(default),
            "demo": demo_mode(),
        }
    except Exception as exc:  # noqa: BLE001 - translated below
        raise _translate_errors(exc) from exc


@app.get("/api/instances", dependencies=[Depends(require_api_key)])
def instances(
    region: Optional[str] = Query(default=None),
    name: Optional[str] = Query(default=None, description="Substring of the Name tag (case-insensitive)"),
    state: Optional[List[str]] = Query(default=None, description="Instance state; repeat for several"),
    tag: Optional[List[str]] = Query(default=None, description="Key=Value tag match; repeat for several"),
    verdict: Optional[List[str]] = Query(default=None, description="Health verdict; repeat for several"),
    inspector: Ec2Inspector = Depends(get_inspector),
) -> dict[str, Any]:
    try:
        return inspector.list_instances(
            region or default_region(),
            name=name,
            states=state,
            tags=parse_tag_filters(tag),
            verdicts=verdict,
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate_errors(exc) from exc


@app.get("/api/instances/{instance_id}", dependencies=[Depends(require_api_key)])
def instance_detail(
    instance_id: str,
    region: Optional[str] = Query(default=None),
    inspector: Ec2Inspector = Depends(get_inspector),
) -> dict[str, Any]:
    try:
        return inspector.get_instance(region or default_region(), instance_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate_errors(exc) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
