"""REST API - the "Power BI service" part: workspaces, datasets, reports,
dashboards, refresh, sharing, row-level security and embedding."""

from __future__ import annotations

import csv
import io
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..dax.compiler import DaxError
from ..engine.filters import FilterError
from ..engine.query import QueryError, SemanticQuery, parse_select
from ..model.schema import SemanticModel
from ..powerquery.refresh import RefreshError
from ..powerquery.steps import StepError
from .core import DatasetStore, ServiceError, Settings, make_store
from .db import WORKSPACE_ROLES, Dashboard, Dataset, Membership, RefreshRun, Report, User, Workspace
from .scheduler import RefreshScheduler
from .security import TokenError, decode_token, hash_password, issue_token, verify_password

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
EDIT_ROLES = ("Admin", "Member", "Contributor")


# ---------------------------------------------------------------- auth --
@dataclass
class Principal:
    kind: str  # user | embed
    user: User | None
    claims: dict[str, Any]

    @property
    def email(self) -> str | None:
        if self.user:
            return self.user.email
        return self.claims.get("username")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> DatasetStore:
    return request.app.state.store


def get_session(request: Request):
    s = request.app.state.sessions()
    try:
        yield s
    finally:
        s.close()


def principal(request: Request, s: Session = Depends(get_session)) -> Principal:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    if not token:
        raise HTTPException(401, "authentication required")
    try:
        claims = decode_token(_settings(request).secret, token)
    except TokenError as e:
        raise HTTPException(401, f"invalid token: {e}") from e
    if claims.get("kind") == "embed":
        return Principal("embed", None, claims)
    user = s.get(User, int(claims.get("sub", 0)))
    if user is None:
        raise HTTPException(401, "unknown user")
    return Principal("user", user, claims)


def user_principal(p: Principal = Depends(principal)) -> Principal:
    if p.kind != "user":
        raise HTTPException(403, "embed tokens cannot call this endpoint")
    return p


def workspace_role(s: Session, p: Principal, workspace_id: int) -> str | None:
    if p.user is None:
        return None
    if p.user.is_admin:
        return "Admin"
    m = s.scalar(select(Membership).where(Membership.workspace_id == workspace_id, Membership.user_id == p.user.id))
    return m.role if m else None


def require_role(s: Session, p: Principal, workspace_id: int, allowed: tuple[str, ...] = WORKSPACE_ROLES) -> str:
    role = workspace_role(s, p, workspace_id)
    if role is None:
        raise HTTPException(404, "workspace not found")
    if role not in allowed:
        raise HTTPException(403, f"requires workspace role: {', '.join(allowed)}")
    return role


def _get(s: Session, cls, id_: int):
    obj = s.get(cls, id_)
    if obj is None:
        raise HTTPException(404, f"{cls.__name__.lower()} not found")
    return obj


# --------------------------------------------------------------- bodies --
class LoginBody(BaseModel):
    email: str
    password: str


class UserBody(BaseModel):
    email: str
    password: str = Field(min_length=8)
    name: str = ""
    is_admin: bool = Field(default=False, alias="isAdmin")


class WorkspaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class MemberBody(BaseModel):
    email: str
    role: str


class DatasetBody(BaseModel):
    name: str | None = None
    definition: dict[str, Any]


class ScheduleBody(BaseModel):
    cron: str | None


class ReportBody(BaseModel):
    name: str
    dataset_id: int = Field(alias="datasetId")
    definition: dict[str, Any] = Field(
        default_factory=lambda: {"pages": [{"name": "page1", "displayName": "Page 1", "visuals": []}]}
    )


class ReportUpdate(BaseModel):
    name: str | None = None
    definition: dict[str, Any] | None = None


class QueryBody(BaseModel):
    select: list[Any]
    filters: list[dict[str, Any]] = Field(default_factory=list)
    order_by: list[dict[str, Any]] = Field(default_factory=list, alias="orderBy")
    top: int | None = Field(default=None, ge=0, le=1_000_000)
    # "View as" for authors (Power BI's "test as role")
    as_roles: list[str] | None = Field(default=None, alias="asRoles")
    as_user: str | None = Field(default=None, alias="asUser")
    format: str | None = None  # csv | xlsx for exports


class PreviewBody(BaseModel):
    table: str
    upto: int | None = None
    limit: int = Field(default=100, ge=1, le=10000)
    definition: dict[str, Any] | None = None


class EmbedTokenBody(BaseModel):
    username: str | None = None
    roles: list[str] = Field(default_factory=list)
    lifetime_minutes: int = Field(default=60, alias="lifetimeMinutes", ge=1, le=24 * 60)
    allow_edit: bool = Field(default=False, alias="allowEdit")


class DashboardBody(BaseModel):
    name: str
    tiles: list[dict[str, Any]] = Field(default_factory=list)


# ------------------------------------------------------------ serialize --
def ws_json(w: Workspace, role: str | None = None) -> dict[str, Any]:
    return {"id": w.id, "name": w.name, "description": w.description, "role": role}


def ds_json(d: Dataset, full: bool = False) -> dict[str, Any]:
    out = {
        "id": d.id,
        "workspaceId": d.workspace_id,
        "name": d.name,
        "status": d.status,
        "refreshSchedule": d.refresh_schedule,
        "lastRefresh": d.last_refresh,
        "updatedAt": d.updated_at.isoformat() if d.updated_at else None,
    }
    if full:
        out["definition"] = d.definition
    return out


def report_json(r: Report, full: bool = False) -> dict[str, Any]:
    out = {
        "id": r.id,
        "workspaceId": r.workspace_id,
        "datasetId": r.dataset_id,
        "name": r.name,
        "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
    }
    if full:
        out["definition"] = r.definition
    return out


def _validate_model(definition: dict[str, Any]) -> SemanticModel:
    try:
        return SemanticModel.model_validate(definition)
    except ValidationError as e:
        raise HTTPException(422, f"invalid semantic model: {e}") from e


# ---------------------------------------------------------------- router --
api = APIRouter(prefix="/api")


@api.post("/auth/login")
def login(body: LoginBody, request: Request, s: Session = Depends(get_session)):
    user = s.scalar(select(User).where(User.email == body.email.lower().strip()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid email or password")
    st = _settings(request)
    token = issue_token(st.secret, {"sub": str(user.id), "kind": "user"}, st.token_ttl)
    return {"token": token, "user": {"id": user.id, "email": user.email, "name": user.name, "isAdmin": user.is_admin}}


@api.get("/me")
def me(p: Principal = Depends(principal)):
    if p.kind == "embed":
        return {"kind": "embed", "reportId": p.claims.get("rid"), "username": p.claims.get("username")}
    u = p.user
    return {"kind": "user", "id": u.id, "email": u.email, "name": u.name, "isAdmin": u.is_admin}


@api.get("/users")
def list_users(p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    if not p.user.is_admin:
        raise HTTPException(403, "admin only")
    return [{"id": u.id, "email": u.email, "name": u.name, "isAdmin": u.is_admin} for u in s.scalars(select(User))]


@api.post("/users", status_code=201)
def create_user(body: UserBody, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    if not p.user.is_admin:
        raise HTTPException(403, "admin only")
    email = body.email.lower().strip()
    if s.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "user already exists")
    u = User(email=email, name=body.name, password_hash=hash_password(body.password), is_admin=body.is_admin)
    s.add(u)
    s.commit()
    return {"id": u.id, "email": u.email, "name": u.name, "isAdmin": u.is_admin}


# workspaces
@api.get("/workspaces")
def list_workspaces(p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    out = []
    for w in s.scalars(select(Workspace).order_by(Workspace.name)):
        role = workspace_role(s, p, w.id)
        if role:
            out.append(ws_json(w, role))
    return out


@api.post("/workspaces", status_code=201)
def create_workspace(body: WorkspaceBody, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    if s.scalar(select(Workspace).where(Workspace.name == body.name)):
        raise HTTPException(409, "a workspace with this name exists")
    w = Workspace(name=body.name, description=body.description)
    s.add(w)
    s.flush()
    s.add(Membership(workspace_id=w.id, user_id=p.user.id, role="Admin"))
    s.commit()
    return ws_json(w, "Admin")


@api.get("/workspaces/{wid}")
def get_workspace(wid: int, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    role = require_role(s, p, wid)
    w = _get(s, Workspace, wid)
    datasets = s.scalars(select(Dataset).where(Dataset.workspace_id == wid).order_by(Dataset.name))
    reports = s.scalars(select(Report).where(Report.workspace_id == wid).order_by(Report.name))
    dashboards = s.scalars(select(Dashboard).where(Dashboard.workspace_id == wid).order_by(Dashboard.name))
    out = ws_json(w, role)
    out["datasets"] = [ds_json(d) for d in datasets]
    out["reports"] = [report_json(r) for r in reports]
    out["dashboards"] = [{"id": d.id, "name": d.name} for d in dashboards]
    if role in ("Admin", "Member"):
        out["members"] = [{"email": m.user.email, "name": m.user.name, "role": m.role} for m in w.members]
    return out


@api.delete("/workspaces/{wid}", status_code=204)
def delete_workspace(
    wid: int, request: Request, p: Principal = Depends(user_principal), s: Session = Depends(get_session)
):
    require_role(s, p, wid, ("Admin",))
    for d in s.scalars(select(Dataset).where(Dataset.workspace_id == wid)):
        _store(request).delete_files(d.id)
        request.app.state.scheduler.unschedule(d.id)
    s.delete(_get(s, Workspace, wid))
    s.commit()
    return Response(status_code=204)


@api.put("/workspaces/{wid}/members")
def set_member(wid: int, body: MemberBody, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    role = require_role(s, p, wid, ("Admin", "Member"))
    if body.role not in WORKSPACE_ROLES and body.role != "None":
        raise HTTPException(422, f"role must be one of {WORKSPACE_ROLES} or None")
    if role == "Member" and body.role == "Admin":
        raise HTTPException(403, "members cannot grant Admin")
    user = s.scalar(select(User).where(User.email == body.email.lower().strip()))
    if user is None:
        raise HTTPException(404, "user not found")
    m = s.scalar(select(Membership).where(Membership.workspace_id == wid, Membership.user_id == user.id))
    if body.role == "None":
        if m:
            s.delete(m)
    elif m:
        m.role = body.role
    else:
        s.add(Membership(workspace_id=wid, user_id=user.id, role=body.role))
    s.commit()
    return {"email": user.email, "role": body.role}


# datasets
@api.post("/workspaces/{wid}/datasets", status_code=201)
def create_dataset(
    wid: int,
    body: DatasetBody,
    request: Request,
    p: Principal = Depends(user_principal),
    s: Session = Depends(get_session),
):
    require_role(s, p, wid, EDIT_ROLES)
    model = _validate_model(body.definition)
    _store(request).check_model(model)
    d = Dataset(
        workspace_id=wid,
        name=body.name or model.name,
        definition=model.model_dump(by_alias=True, exclude_none=True, mode="json"),
    )
    s.add(d)
    s.commit()
    return ds_json(d, full=True)


def _dataset_for(s: Session, p: Principal, did: int, allowed=WORKSPACE_ROLES) -> tuple[Dataset, str]:
    d = _get(s, Dataset, did)
    return d, require_role(s, p, d.workspace_id, allowed)


@api.get("/datasets/{did}")
def get_dataset(did: int, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    d, role = _dataset_for(s, p, did)
    return ds_json(d, full=role in EDIT_ROLES)


@api.put("/datasets/{did}")
def update_dataset(
    did: int,
    body: DatasetBody,
    request: Request,
    p: Principal = Depends(user_principal),
    s: Session = Depends(get_session),
):
    d, _ = _dataset_for(s, p, did, EDIT_ROLES)
    model = _validate_model(body.definition)
    _store(request).check_model(model)
    d.definition = model.model_dump(by_alias=True, exclude_none=True, mode="json")
    if body.name:
        d.name = body.name
    s.commit()
    return ds_json(d, full=True)


@api.delete("/datasets/{did}", status_code=204)
def delete_dataset(
    did: int, request: Request, p: Principal = Depends(user_principal), s: Session = Depends(get_session)
):
    d, _ = _dataset_for(s, p, did, EDIT_ROLES)
    request.app.state.scheduler.unschedule(d.id)
    _store(request).delete_files(d.id)
    s.delete(d)
    s.commit()
    return Response(status_code=204)


@api.post("/datasets/{did}/refresh")
def refresh_dataset(
    did: int, request: Request, p: Principal = Depends(user_principal), s: Session = Depends(get_session)
):
    _dataset_for(s, p, did, EDIT_ROLES)
    run = _store(request).refresh(did, trigger="manual")
    out = {"id": run.id, "status": run.status, "details": run.details, "error": run.error}
    return JSONResponse(out, status_code=200 if run.status == "succeeded" else 422)


@api.get("/datasets/{did}/refreshes")
def refresh_history(did: int, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    _dataset_for(s, p, did)
    runs = s.scalars(select(RefreshRun).where(RefreshRun.dataset_id == did).order_by(RefreshRun.id.desc()).limit(50))
    return [
        {
            "id": r.id,
            "trigger": r.trigger,
            "status": r.status,
            "startedAt": r.started_at.isoformat(),
            "finishedAt": r.finished_at.isoformat() if r.finished_at else None,
            "error": r.error,
            "details": r.details,
        }
        for r in runs
    ]


@api.put("/datasets/{did}/schedule")
def set_schedule(
    did: int,
    body: ScheduleBody,
    request: Request,
    p: Principal = Depends(user_principal),
    s: Session = Depends(get_session),
):
    d, _ = _dataset_for(s, p, did, EDIT_ROLES)
    sched: RefreshScheduler = request.app.state.scheduler
    if body.cron:
        try:
            sched.schedule(did, body.cron)
        except ValueError as e:
            raise HTTPException(422, f"invalid cron expression: {e}") from e
    else:
        sched.unschedule(did)
    d.refresh_schedule = body.cron or None
    s.commit()
    return ds_json(d)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._ -]{1,200}$")


@api.post("/datasets/{did}/files", status_code=201)
async def upload_file(
    did: int,
    request: Request,
    file: UploadFile = File(...),
    p: Principal = Depends(user_principal),
    s: Session = Depends(get_session),
):
    _dataset_for(s, p, did, EDIT_ROLES)
    name = Path(file.filename or "").name
    if not _SAFE_NAME.match(name) or name.startswith("."):
        raise HTTPException(422, "invalid file name")
    dest = _store(request).data_dir(did) / "data" / name
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            fh.write(chunk)
    return {"name": name, "path": f"data/{name}", "bytes": size}


@api.get("/datasets/{did}/files")
def list_files(did: int, request: Request, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    _dataset_for(s, p, did, EDIT_ROLES)
    d = _store(request).data_dir(did) / "data"
    return [
        {"name": f.name, "path": f"data/{f.name}", "bytes": f.stat().st_size}
        for f in sorted(d.iterdir())
        if f.is_file()
    ]


@api.post("/datasets/{did}/preview")
def preview(
    did: int,
    body: PreviewBody,
    request: Request,
    p: Principal = Depends(user_principal),
    s: Session = Depends(get_session),
):
    d, _ = _dataset_for(s, p, did, EDIT_ROLES)
    if body.definition is not None:
        _validate_model(body.definition)
    return _store(request).preview(d, body.table, body.upto, body.limit, body.definition)


def _effective_identity(s: Session, p: Principal, d: Dataset, body: QueryBody) -> tuple[list[str] | None, str | None]:
    """Which RLS roles apply. Mirrors Power BI: RLS binds Viewers, embed
    tokens carry an explicit effective identity, authors may "view as"."""
    model = SemanticModel.model_validate(d.definition)
    if p.kind == "embed":
        if p.claims.get("did") != d.id:
            raise HTTPException(403, "embed token is not valid for this dataset")
        roles = p.claims.get("roles") or []
        if model.roles and not roles:
            raise HTTPException(403, "this dataset uses row-level security; the embed token needs roles")
        return roles, p.claims.get("username")
    role = require_role(s, p, d.workspace_id)
    if role in EDIT_ROLES:
        if body.as_roles:
            return body.as_roles, body.as_user or p.email
        return None, p.email
    if not model.roles:
        return None, p.email
    email = (p.email or "").lower()
    mine = [r.name for r in model.roles if email in {m.lower() for m in r.members}]
    if not mine:
        raise HTTPException(403, "you are not a member of any row-level security role of this dataset")
    return mine, p.email


def _run_query(did: int, body: QueryBody, request: Request, p: Principal, s: Session):
    d = _get(s, Dataset, did)
    roles, user = _effective_identity(s, p, d, body)
    try:
        q = SemanticQuery(
            select=parse_select(body.select),
            filters=body.filters,
            order_by=[(o["field"], str(o.get("direction", "asc")).lower() == "desc") for o in body.order_by],
            top=body.top,
        )
        return _store(request).query(d, q, roles, user)
    except (QueryError, DaxError, FilterError) as e:
        raise HTTPException(400, str(e).strip("'\"")) from e


@api.post("/datasets/{did}/query")
def query(
    did: int, body: QueryBody, request: Request, p: Principal = Depends(principal), s: Session = Depends(get_session)
):
    res = _run_query(did, body, request, p, s)
    out = res.as_dict()
    if p.kind == "user" and workspace_role(s, p, _get(s, Dataset, did).workspace_id) in EDIT_ROLES:
        out["sql"] = res.sql
    return out


@api.post("/datasets/{did}/export")
def export(
    did: int, body: QueryBody, request: Request, p: Principal = Depends(principal), s: Session = Depends(get_session)
):
    res = _run_query(did, body, request, p, s)
    names = [c["name"] for c in res.columns]
    if (body.format or "csv") == "xlsx":
        from openpyxl import Workbook

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Data")
        ws.append(names)
        for r in res.rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return Response(
            buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="export.xlsx"'},
        )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(names)
    w.writerows(res.rows)
    return Response(
        buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="export.csv"'}
    )


@api.get("/datasets/{did}/schema")
def schema(did: int, request: Request, p: Principal = Depends(principal), s: Session = Depends(get_session)):
    d = _get(s, Dataset, did)
    if p.kind == "embed":
        if p.claims.get("did") != d.id:
            raise HTTPException(403, "embed token is not valid for this dataset")
    else:
        require_role(s, p, d.workspace_id)
    return _store(request).schema(d)


# reports
@api.post("/workspaces/{wid}/reports", status_code=201)
def create_report(
    wid: int, body: ReportBody, p: Principal = Depends(user_principal), s: Session = Depends(get_session)
):
    require_role(s, p, wid, EDIT_ROLES)
    d = _get(s, Dataset, body.dataset_id)
    # a report may use a dataset from another workspace the author can read (shared dataset)
    require_role(s, p, d.workspace_id)
    r = Report(workspace_id=wid, dataset_id=d.id, name=body.name, definition=body.definition)
    s.add(r)
    s.commit()
    return report_json(r, full=True)


def _report_access(s: Session, p: Principal, r: Report, edit: bool = False) -> None:
    if p.kind == "embed":
        if p.claims.get("rid") != r.id:
            raise HTTPException(403, "embed token is not valid for this report")
        if edit and not p.claims.get("edit"):
            raise HTTPException(403, "embed token does not allow editing")
        return
    require_role(s, p, r.workspace_id, EDIT_ROLES if edit else WORKSPACE_ROLES)


@api.get("/reports/{rid}")
def get_report(rid: int, p: Principal = Depends(principal), s: Session = Depends(get_session)):
    r = _get(s, Report, rid)
    _report_access(s, p, r)
    out = report_json(r, full=True)
    out["canEdit"] = (
        bool(p.claims.get("edit")) if p.kind == "embed" else workspace_role(s, p, r.workspace_id) in EDIT_ROLES
    )
    return out


@api.put("/reports/{rid}")
def update_report(rid: int, body: ReportUpdate, p: Principal = Depends(principal), s: Session = Depends(get_session)):
    r = _get(s, Report, rid)
    _report_access(s, p, r, edit=True)
    if body.name:
        r.name = body.name
    if body.definition is not None:
        r.definition = body.definition
    s.commit()
    return report_json(r, full=True)


@api.delete("/reports/{rid}", status_code=204)
def delete_report(rid: int, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    r = _get(s, Report, rid)
    require_role(s, p, r.workspace_id, EDIT_ROLES)
    s.delete(r)
    s.commit()
    return Response(status_code=204)


@api.post("/reports/{rid}/embed-token")
def embed_token(
    rid: int,
    body: EmbedTokenBody,
    request: Request,
    p: Principal = Depends(user_principal),
    s: Session = Depends(get_session),
):
    """Like Power BI's GenerateToken: a short-lived token for one report,
    optionally with an effective identity for row-level security."""
    r = _get(s, Report, rid)
    require_role(s, p, r.workspace_id, ("Admin", "Member"))
    d = _get(s, Dataset, r.dataset_id)
    model = SemanticModel.model_validate(d.definition)
    for role in body.roles:
        if model.role(role) is None:
            raise HTTPException(422, f"unknown role '{role}'")
    if model.roles and not body.roles:
        raise HTTPException(
            422, "dataset has row-level security: specify roles (and username) for the effective identity"
        )
    claims = {
        "kind": "embed",
        "rid": r.id,
        "did": d.id,
        "roles": body.roles,
        "username": body.username,
        "edit": body.allow_edit,
        "by": p.user.id,
    }
    token = issue_token(_settings(request).secret, claims, body.lifetime_minutes * 60)
    return {
        "token": token,
        "embedUrl": f"/embed/report/{r.id}",
        "reportId": r.id,
        "expiresInMinutes": body.lifetime_minutes,
    }


# dashboards
@api.post("/workspaces/{wid}/dashboards", status_code=201)
def create_dashboard(
    wid: int, body: DashboardBody, p: Principal = Depends(user_principal), s: Session = Depends(get_session)
):
    require_role(s, p, wid, EDIT_ROLES)
    d = Dashboard(workspace_id=wid, name=body.name, tiles=body.tiles)
    s.add(d)
    s.commit()
    return {"id": d.id, "name": d.name, "tiles": d.tiles, "workspaceId": wid}


@api.get("/dashboards/{bid}")
def get_dashboard(bid: int, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    d = _get(s, Dashboard, bid)
    role = require_role(s, p, d.workspace_id)
    tiles = []
    for t in d.tiles:
        r = s.get(Report, t.get("reportId", 0))
        if r is None or workspace_role(s, p, r.workspace_id) is None:
            continue
        tiles.append({**t, "datasetId": r.dataset_id})
    return {"id": d.id, "name": d.name, "workspaceId": d.workspace_id, "tiles": tiles, "canEdit": role in EDIT_ROLES}


@api.put("/dashboards/{bid}")
def update_dashboard(
    bid: int, body: DashboardBody, p: Principal = Depends(user_principal), s: Session = Depends(get_session)
):
    d = _get(s, Dashboard, bid)
    require_role(s, p, d.workspace_id, EDIT_ROLES)
    d.name = body.name
    d.tiles = body.tiles
    s.commit()
    return {"id": d.id, "name": d.name, "tiles": d.tiles}


@api.delete("/dashboards/{bid}", status_code=204)
def delete_dashboard(bid: int, p: Principal = Depends(user_principal), s: Session = Depends(get_session)):
    d = _get(s, Dashboard, bid)
    require_role(s, p, d.workspace_id, EDIT_ROLES)
    s.delete(d)
    s.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ app --
def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    sessions, store = make_store(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.scheduler.shutdown()

    app = FastAPI(
        title="OpenBI", version=__version__, description="Open-source business intelligence service", lifespan=lifespan
    )
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.store = store
    app.state.scheduler = RefreshScheduler(store, sessions, start=start_scheduler)

    @app.exception_handler(ServiceError)
    def _service_error(_: Request, e: ServiceError):
        return JSONResponse({"detail": e.message}, status_code=e.status)

    @app.exception_handler(RefreshError)
    @app.exception_handler(StepError)
    def _refresh_error(_: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=400)

    app.include_router(api)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/embed/report/{rid}", include_in_schema=False)
    def embed_page(rid: int):
        return FileResponse(STATIC / "embed.html")

    @app.get("/healthz", include_in_schema=False)
    def health():
        return {"ok": True}

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(STATIC / "index.html")

    return app
