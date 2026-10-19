"""Service internals: settings, dataset storage, refresh and query execution."""

from __future__ import annotations

import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy.orm import sessionmaker

from ..engine.query import QueryEngine, QueryResult, SemanticQuery
from ..model.schema import SemanticModel
from ..powerquery.refresh import describe, open_model_db, preview_table, refresh_model
from .db import Dataset, RefreshRun, make_sessionmaker


@dataclass
class Settings:
    home: Path
    secret: str
    database_url: str
    allow_database_sources: bool = False
    query_timeout: float = 30.0
    token_ttl: int = 12 * 3600

    @classmethod
    def from_env(cls, home: str | Path | None = None) -> Settings:
        root = Path(home or os.environ.get("OPENBI_HOME", "./openbi-data")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        secret = os.environ.get("OPENBI_SECRET")
        if not secret:
            secret_file = root / "secret.key"
            if not secret_file.exists():
                secret_file.write_text(secrets.token_urlsafe(48))
                secret_file.chmod(0o600)
            secret = secret_file.read_text().strip()
        return cls(
            home=root,
            secret=secret,
            database_url=os.environ.get("OPENBI_DATABASE_URL", f"sqlite:///{root / 'openbi.sqlite'}"),
            allow_database_sources=os.environ.get("OPENBI_ALLOW_DATABASE_SOURCES", "") in ("1", "true", "yes"),
            query_timeout=float(os.environ.get("OPENBI_QUERY_TIMEOUT", "30")),
        )


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class DatasetStore:
    """Owns each dataset's files and its cached read-only DuckDB connection."""

    def __init__(self, settings: Settings, sessions: sessionmaker):
        self.settings = settings
        self.sessions = sessions
        self._lock = threading.Lock()
        self._cons: dict[int, tuple[float, duckdb.DuckDBPyConnection]] = {}
        self._refresh_locks: dict[int, threading.Lock] = {}

    def data_dir(self, dataset_id: int) -> Path:
        d = self.settings.home / "datasets" / str(dataset_id)
        (d / "data").mkdir(parents=True, exist_ok=True)
        return d

    def db_path(self, dataset_id: int) -> Path:
        return self.data_dir(dataset_id) / "model.duckdb"

    def delete_files(self, dataset_id: int) -> None:
        self._drop(dataset_id)
        shutil.rmtree(self.settings.home / "datasets" / str(dataset_id), ignore_errors=True)

    def _drop(self, dataset_id: int) -> None:
        with self._lock:
            old = self._cons.pop(dataset_id, None)
        if old:
            old[1].close()

    def connection(self, dataset_id: int) -> duckdb.DuckDBPyConnection:
        path = self.db_path(dataset_id)
        if not path.exists():
            raise ServiceError(409, "dataset has not been refreshed yet")
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._cons.get(dataset_id)
            if cached and cached[0] == mtime:
                return cached[1].cursor()
            con = open_model_db(path)
            self._cons[dataset_id] = (mtime, con)
        if cached:
            cached[1].close()
        return con.cursor()

    def check_model(self, model: SemanticModel) -> None:
        if not self.settings.allow_database_sources and any(t.source.kind == "database" for t in model.tables):
            raise ServiceError(400, "database sources are disabled on this server (OPENBI_ALLOW_DATABASE_SOURCES)")

    # -- refresh -------------------------------------------------------
    def refresh(self, dataset_id: int, trigger: str = "manual") -> RefreshRun:
        lock = self._refresh_locks.setdefault(dataset_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ServiceError(409, "a refresh is already running for this dataset")
        try:
            with self.sessions() as s:
                ds = s.get(Dataset, dataset_id)
                if ds is None:
                    raise ServiceError(404, "dataset not found")
                model = SemanticModel.model_validate(ds.definition)
                run = RefreshRun(dataset_id=dataset_id, trigger=trigger, status="running")
                ds.status = "refreshing"
                s.add(run)
                s.commit()
                run_id = run.id
            self.check_model(model)
            base = self.data_dir(dataset_id)
            try:
                result = refresh_model(model, self.db_path(dataset_id), base_dir=base, allowed_root=base / "data")
                status, details, error = "succeeded", result.as_dict(), None
            except Exception as e:  # noqa: BLE001 - recorded in refresh history
                status, details, error = "failed", None, str(e)
            with self.sessions() as s:
                run = s.get(RefreshRun, run_id)
                ds = s.get(Dataset, dataset_id)
                run.status = status
                run.details = details
                run.error = error
                run.finished_at = datetime.now(UTC)
                if ds is not None:
                    ds.status = "ready" if status == "succeeded" else "failed"
                    ds.last_refresh = {
                        "status": status,
                        "at": run.finished_at.isoformat(),
                        "error": error,
                        **(details or {}),
                    }
                s.commit()
                return run
        finally:
            lock.release()

    # -- queries -------------------------------------------------------
    def query(
        self,
        ds: Dataset,
        q: SemanticQuery,
        roles: list[str] | None,
        user: str | None,
    ) -> QueryResult:
        model = SemanticModel.model_validate(ds.definition)
        engine = QueryEngine(model, roles=roles, user=user)
        return engine.execute(self.connection(ds.id), q, timeout=self.settings.query_timeout)

    def schema(self, ds: Dataset) -> dict[str, Any]:
        model = SemanticModel.model_validate(ds.definition)
        try:
            physical = describe(self.connection(ds.id), model)
        except ServiceError:
            physical = {t.name: [] for t in model.tables}
        tables = []
        for t in model.tables:
            meta = {c.name.lower(): c for c in t.columns}
            cols = []
            for pc in physical.get(t.name, []):
                m = meta.get(pc["name"].lower())
                cols.append(
                    {
                        "name": pc["name"],
                        "type": pc["type"],
                        "isHidden": bool(m and m.is_hidden),
                        "isCalculated": bool(m and m.expression),
                        "formatString": m.format_string if m else None,
                        "sortByColumn": m.sort_by_column if m else None,
                    }
                )
            if not physical.get(t.name):
                cols = [
                    {"name": c.name, "type": c.data_type.value if c.data_type else None, "isHidden": c.is_hidden}
                    for c in t.columns
                ]
            tables.append(
                {
                    "name": t.name,
                    "isHidden": t.is_hidden,
                    "mode": t.mode,
                    "columns": cols,
                    "measures": [
                        {
                            "name": m.name,
                            "expression": m.expression,
                            "formatString": m.format_string,
                            "displayFolder": m.display_folder,
                            "isHidden": m.is_hidden,
                        }
                        for m in t.measures
                    ],
                }
            )
        rels = [r.model_dump(by_alias=True) for r in model.relationships]
        return {"name": model.name, "tables": tables, "relationships": rels, "roles": [r.name for r in model.roles]}

    def preview(self, ds: Dataset, table: str, upto: int | None, limit: int, definition: dict | None = None) -> dict:
        model = SemanticModel.model_validate(definition or ds.definition)
        self.check_model(model)
        base = self.data_dir(ds.id)
        return preview_table(model, table, base, base / "data", upto=upto, limit=limit)


def make_store(settings: Settings) -> tuple[sessionmaker, DatasetStore]:
    sessions = make_sessionmaker(settings.database_url)
    return sessions, DatasetStore(settings, sessions)
