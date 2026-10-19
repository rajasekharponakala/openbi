"""Command line interface (Typer)."""

from __future__ import annotations

import getpass
import json
import shutil
from pathlib import Path

import typer
from sqlalchemy import select

app = typer.Typer(help="OpenBI - open-source business intelligence", no_args_is_help=True)


def _settings(home: Path | None):
    from .service.core import Settings

    return Settings.from_env(home)


def _version(value: bool) -> None:
    if value:
        from . import __version__

        typer.echo(f"openbi {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """OpenBI - open-source business intelligence."""


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Port"),
    home: Path | None = typer.Option(None, envvar="OPENBI_HOME", help="Data directory"),
) -> None:
    """Run the web service (report designer, viewer, REST API)."""
    import uvicorn

    from .service.api import create_app

    uvicorn.run(create_app(_settings(home)), host=host, port=port)


@app.command("create-user")
def create_user(
    email: str,
    name: str = typer.Option("", help="Display name"),
    admin: bool = typer.Option(False, help="Grant server administrator rights"),
    password: str | None = typer.Option(None, help="Password (prompted when omitted)"),
    home: Path | None = typer.Option(None, envvar="OPENBI_HOME"),
) -> None:
    """Create a user account."""
    from .service.core import make_store
    from .service.db import User
    from .service.security import hash_password

    pw = password or getpass.getpass("Password: ")
    if len(pw) < 8:
        raise typer.BadParameter("password must be at least 8 characters")
    sessions, _ = make_store(_settings(home))
    with sessions() as s:
        if s.scalar(select(User).where(User.email == email.lower())):
            typer.echo("user already exists", err=True)
            raise typer.Exit(1)
        s.add(User(email=email.lower(), name=name, password_hash=hash_password(pw), is_admin=admin))
        s.commit()
    typer.echo(f"created {email}")


@app.command()
def sample(path: Path, orders: int = typer.Option(4000, help="Number of sales orders to generate")) -> None:
    """Write the sample sales project (data, model and report) to PATH."""
    from .samples import create_sample_project

    create_sample_project(path, orders=orders)
    typer.echo(f"sample project written to {path}")


@app.command()
def validate(project: Path) -> None:
    """Validate a project's model and compile every measure."""
    from .dax.compiler import LeafRegistry, MeasureCompiler, compile_row
    from .engine.graph import ModelGraph
    from .model.project import load_project

    p = load_project(project)
    graph = ModelGraph(p.model)
    errors = 0
    for t in p.model.tables:
        for m in t.measures:
            try:
                MeasureCompiler(p.model, graph, LeafRegistry()).compile_measure(m.name)
            except ValueError as e:
                errors += 1
                typer.echo(f"measure [{m.name}]: {e}", err=True)
        for c in t.columns:
            if c.expression:
                try:
                    compile_row(p.model, graph, t.name, c.expression)
                except ValueError as e:
                    errors += 1
                    typer.echo(f"column {t.name}[{c.name}]: {e}", err=True)
    if errors:
        raise typer.Exit(1)
    typer.echo(f"{p.name}: {len(p.model.tables)} tables, {sum(len(t.measures) for t in p.model.tables)} measures OK")


@app.command()
def refresh(project: Path) -> None:
    """Refresh a project locally into PROJECT/.openbi/model.duckdb."""
    from .model.project import load_project
    from .powerquery.refresh import refresh_model

    p = load_project(project)
    res = refresh_model(p.model, project / ".openbi" / "model.duckdb", base_dir=project)
    for t in res.tables:
        typer.echo(f"{t.name:<20} {t.mode:<12} {t.rows if t.rows is not None else '-':>10} rows  {t.seconds:.2f}s")
    typer.echo(f"refreshed in {res.seconds:.2f}s")


@app.command()
def query(
    project: Path,
    fields: list[str] = typer.Argument(..., help='Columns and measures, e.g. "Product[Category]" "[Total Sales]"'),
    filter: list[str] = typer.Option([], "--filter", "-f", help="Filter JSON (Power BI filter schema)"),
    role: list[str] = typer.Option([], "--role", help="Evaluate as RLS role"),
    user: str | None = typer.Option(None, help="USERPRINCIPALNAME() for RLS"),
    top: int | None = typer.Option(None),
    sql: bool = typer.Option(False, help="Print the generated SQL"),
) -> None:
    """Run a semantic query against a locally refreshed project."""
    from .engine.query import QueryEngine, SemanticQuery, parse_select
    from .model.project import load_project
    from .powerquery.refresh import open_model_db

    p = load_project(project)
    con = open_model_db(project / ".openbi" / "model.duckdb")
    engine = QueryEngine(p.model, roles=role or None, user=user)
    res = engine.execute(con, SemanticQuery(parse_select(fields), [json.loads(f) for f in filter], [], top))
    if sql:
        typer.echo(res.sql + "\n")
    names = [c["name"] for c in res.columns]
    rows = [["" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v)) for v in r] for r in res.rows]
    widths = [max([len(n)] + [len(r[i]) for r in rows]) for i, n in enumerate(names)]
    typer.echo("  ".join(n.ljust(w) for n, w in zip(names, widths, strict=True)))
    typer.echo("  ".join("-" * w for w in widths))
    for r in rows:
        typer.echo(
            "  ".join(
                v.rjust(w) if v[:1].isdigit() or v[:1] == "-" else v.ljust(w) for v, w in zip(r, widths, strict=True)
            )
        )


def _publish(
    sessions, store, project_path: Path, workspace: str, owner_id: int, refresh: bool = True
) -> tuple[int, list[int]]:
    from .model.project import load_project
    from .service.db import Dataset, Membership, Report, Workspace

    p = load_project(project_path)
    with sessions() as s:
        ws = s.scalar(select(Workspace).where(Workspace.name == workspace))
        if ws is None:
            ws = Workspace(name=workspace, description=f"Published from {project_path.name}")
            s.add(ws)
            s.flush()
            s.add(Membership(workspace_id=ws.id, user_id=owner_id, role="Admin"))
        ds = Dataset(
            workspace_id=ws.id,
            name=p.model.name,
            definition=p.model.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )
        s.add(ds)
        s.flush()
        report_ids = []
        for name, definition in p.reports.items():
            r = Report(workspace_id=ws.id, dataset_id=ds.id, name=definition.get("name", name), definition=definition)
            s.add(r)
            s.flush()
            report_ids.append(r.id)
        s.commit()
        ds_id = ds.id
    src = project_path / "data"
    if src.exists():
        shutil.copytree(src, store.data_dir(ds_id) / "data", dirs_exist_ok=True)
    if refresh:
        run = store.refresh(ds_id, trigger="api")
        if run.status != "succeeded":
            typer.echo(f"refresh failed: {run.error}", err=True)
    return ds_id, report_ids


@app.command()
def publish(
    project: Path,
    workspace: str = typer.Option(..., help="Target workspace (created when missing)"),
    owner: str = typer.Option(..., help="Email of the owning user"),
    home: Path | None = typer.Option(None, envvar="OPENBI_HOME"),
) -> None:
    """Publish a project folder (model, reports, data files) to the service."""
    from .service.core import make_store
    from .service.db import User

    sessions, store = make_store(_settings(home))
    with sessions() as s:
        u = s.scalar(select(User).where(User.email == owner.lower()))
    if u is None:
        typer.echo("owner not found; create it with `openbi create-user`", err=True)
        raise typer.Exit(1)
    ds_id, reports = _publish(sessions, store, project, workspace, u.id)
    typer.echo(f"published dataset {ds_id} and reports {reports}")


@app.command()
def demo(
    home: Path | None = typer.Option(None, envvar="OPENBI_HOME"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    serve_after: bool = typer.Option(True, "--serve/--no-serve", help="Start the server afterwards"),
) -> None:
    """Seed demo users, a workspace and the sample report, then serve."""
    import tempfile

    from .samples import REGIONS, create_sample_project
    from .service.core import make_store
    from .service.db import Dataset, Membership, User, Workspace
    from .service.security import hash_password

    settings = _settings(home)
    sessions, store = make_store(settings)
    with sessions() as s:
        exists = s.scalar(select(Workspace).where(Workspace.name == "Sales Demo"))
        admin = s.scalar(select(User).where(User.email == "admin@example.com"))
        if admin is None:
            admin = User(
                email="admin@example.com", name="Admin", password_hash=hash_password("openbi-admin"), is_admin=True
            )
            s.add(admin)
            for _, name, email in REGIONS:
                s.add(User(email=email, name=name, password_hash=hash_password("openbi-viewer")))
            s.commit()
        admin_id = admin.id
    if not exists:
        with tempfile.TemporaryDirectory() as tmp:
            create_sample_project(Path(tmp) / "sample")
            ds_id, _ = _publish(sessions, store, Path(tmp) / "sample", "Sales Demo", admin_id)
        with sessions() as s:
            ws = s.scalar(select(Workspace).where(Workspace.name == "Sales Demo"))
            ds = s.get(Dataset, ds_id)
            definition = dict(ds.definition)
            roles = []
            for r in definition.get("roles", []):
                r = dict(r)
                if r["name"] == "Regional Manager":
                    r["members"] = [e for _, _, e in REGIONS]
                roles.append(r)
            definition["roles"] = roles
            ds.definition = definition
            for _, _, email in REGIONS:
                u = s.scalar(select(User).where(User.email == email))
                s.add(Membership(workspace_id=ws.id, user_id=u.id, role="Viewer"))
            s.commit()
    typer.echo("Demo ready:")
    typer.echo("  admin@example.com / openbi-admin      (workspace Admin, sees all data)")
    for region, _, email in REGIONS:
        typer.echo(f"  {email:<22} / openbi-viewer     (Viewer, RLS: {region} only)")
    if serve_after:
        serve(host=host, port=port, home=settings.home)


if __name__ == "__main__":  # pragma: no cover
    app()
