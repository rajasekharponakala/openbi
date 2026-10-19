import pytest
from fastapi.testclient import TestClient

from openbi.samples import MODEL, generate_data
from openbi.service.api import create_app
from openbi.service.core import Settings
from openbi.service.db import User
from openbi.service.security import hash_password


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        home=tmp_path,
        secret="test-secret-0123456789-abcdefghijklmnop",
        database_url=f"sqlite:///{tmp_path / 'meta.sqlite'}",
    )
    app = create_app(settings, start_scheduler=False)
    with app.state.sessions() as s:
        s.add(User(email="admin@example.com", name="Admin", password_hash=hash_password("adminpass1"), is_admin=True))
        for email in ("viewer@example.com", "sam@example.com", "author@example.com"):
            s.add(User(email=email, password_hash=hash_password("password12")))
        s.commit()
    c = TestClient(app)
    c.tmp = tmp_path
    return c


def login(c, email, pw="password12"):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def published(client):
    admin = login(client, "admin@example.com", "adminpass1")
    ws = client.post("/api/workspaces", json={"name": "Sales"}, headers=admin).json()
    model = {
        **MODEL,
        "roles": [
            {**r, "members": ["sam@example.com"]} if r["name"] == "Regional Manager" else r for r in MODEL["roles"]
        ],
    }
    ds = client.post(f"/api/workspaces/{ws['id']}/datasets", json={"definition": model}, headers=admin).json()
    data_dir = client.app.state.store.data_dir(ds["id"]) / "data"
    generate_data(data_dir, orders=300)
    r = client.post(f"/api/datasets/{ds['id']}/refresh", headers=admin)
    assert r.status_code == 200, r.text
    rep = client.post(
        f"/api/workspaces/{ws['id']}/reports", json={"name": "R", "datasetId": ds["id"]}, headers=admin
    ).json()
    return {"admin": admin, "ws": ws["id"], "ds": ds["id"], "report": rep["id"]}


def test_login_and_auth_required(client):
    assert client.get("/api/workspaces").status_code == 401
    assert client.post("/api/auth/login", json={"email": "admin@example.com", "password": "bad"}).status_code == 401
    assert client.get("/api/workspaces", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_workspace_roles(client, published):
    admin, ws = published["admin"], published["ws"]
    viewer = login(client, "viewer@example.com")
    assert client.get(f"/api/workspaces/{ws}", headers=viewer).status_code == 404
    assert (
        client.put(
            f"/api/workspaces/{ws}/members", json={"email": "viewer@example.com", "role": "Viewer"}, headers=admin
        ).status_code
        == 200
    )
    assert client.get(f"/api/workspaces/{ws}", headers=viewer).json()["role"] == "Viewer"
    # viewers cannot edit or see the model definition
    assert client.put(f"/api/reports/{published['report']}", json={"name": "x"}, headers=viewer).status_code == 403
    assert "definition" not in client.get(f"/api/datasets/{published['ds']}", headers=viewer).json()
    assert client.post(f"/api/datasets/{published['ds']}/refresh", headers=viewer).status_code == 403


def test_query_and_rls_for_viewers(client, published):
    admin, ws, ds = published["admin"], published["ws"], published["ds"]
    body = {"select": ["Region[Region]", "[Total Sales]"]}
    all_rows = client.post(f"/api/datasets/{ds}/query", json=body, headers=admin).json()
    assert len(all_rows["rows"]) == 4 and "sql" in all_rows
    client.put(f"/api/workspaces/{ws}/members", json={"email": "sam@example.com", "role": "Viewer"}, headers=admin)
    client.put(f"/api/workspaces/{ws}/members", json={"email": "viewer@example.com", "role": "Viewer"}, headers=admin)
    sam = login(client, "sam@example.com")
    res = client.post(f"/api/datasets/{ds}/query", json=body, headers=sam).json()
    assert [r[0] for r in res["rows"]] == ["South"] and "sql" not in res
    # viewers can't escape RLS with "view as"
    res = client.post(f"/api/datasets/{ds}/query", json={**body, "asRoles": ["West Only"]}, headers=sam).json()
    assert [r[0] for r in res["rows"]] == ["South"]
    # a viewer in no role gets no data
    other = login(client, "viewer@example.com")
    assert client.post(f"/api/datasets/{ds}/query", json=body, headers=other).status_code == 403
    # authors can test a role
    res = client.post(f"/api/datasets/{ds}/query", json={**body, "asRoles": ["West Only"]}, headers=admin).json()
    assert [r[0] for r in res["rows"]] == ["West"]


def test_query_errors_are_400(client, published):
    r = client.post(f"/api/datasets/{published['ds']}/query", json={"select": ["[Nope]"]}, headers=published["admin"])
    assert r.status_code == 400 and "unknown measure" in r.json()["detail"]


def test_embed_token_scope(client, published):
    admin, ds, rep = published["admin"], published["ds"], published["report"]
    assert (
        client.post(f"/api/reports/{rep}/embed-token", json={}, headers=admin).status_code == 422
    )  # RLS model needs roles
    tok = client.post(
        f"/api/reports/{rep}/embed-token", json={"roles": ["West Only"], "username": "x@y.z"}, headers=admin
    ).json()
    emb = {"Authorization": f"Bearer {tok['token']}"}
    assert client.get(f"/api/reports/{rep}", headers=emb).json()["canEdit"] is False
    assert client.put(f"/api/reports/{rep}", json={"name": "hacked"}, headers=emb).status_code == 403
    rows = client.post(f"/api/datasets/{ds}/query", json={"select": ["Region[Region]"]}, headers=emb).json()["rows"]
    assert rows == [["West"]]
    assert client.get("/api/workspaces", headers=emb).status_code == 403
    assert client.get(f"/api/datasets/{ds}/schema", headers=emb).status_code == 200
    # token is bound to its report / dataset
    other = client.post(
        f"/api/workspaces/{published['ws']}/reports", json={"name": "R2", "datasetId": ds}, headers=admin
    ).json()
    assert client.get(f"/api/reports/{other['id']}", headers=emb).status_code == 403


def test_export_csv_and_xlsx(client, published):
    body = {"select": ["Product[Category]", "[Units]"]}
    r = client.post(f"/api/datasets/{published['ds']}/export", json=body, headers=published["admin"])
    assert r.status_code == 200 and r.text.splitlines()[0] == "Product[Category],Units"
    r = client.post(
        f"/api/datasets/{published['ds']}/export", json={**body, "format": "xlsx"}, headers=published["admin"]
    )
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_upload_preview_and_failed_refresh(client, published):
    admin, ds = published["admin"], published["ds"]
    r = client.post(f"/api/datasets/{ds}/files", files={"file": ("../evil.csv", b"a,b\n1,2\n")}, headers=admin)
    assert r.status_code == 201 and r.json()["path"] == "data/evil.csv"
    assert not (client.tmp / "evil.csv").exists()
    definition = client.get(f"/api/datasets/{ds}", headers=admin).json()["definition"]
    definition["tables"].append({"name": "Evil", "source": {"kind": "csv", "path": "data/evil.csv"}})
    prev = client.post(
        f"/api/datasets/{ds}/preview", json={"table": "Evil", "definition": definition}, headers=admin
    ).json()
    assert prev["rows"] == [[1, 2]]
    definition["tables"].append(
        {"name": "Bad", "source": {"kind": "sql", "query": "SELECT * FROM read_csv_auto('/etc/passwd')"}}
    )
    assert client.put(f"/api/datasets/{ds}", json={"definition": definition}, headers=admin).status_code == 200
    r = client.post(f"/api/datasets/{ds}/refresh", headers=admin)
    assert r.status_code == 422 and r.json()["status"] == "failed"
    # the previous refresh is still served
    assert client.post(f"/api/datasets/{ds}/query", json={"select": ["[Units]"]}, headers=admin).status_code == 200
    hist = client.get(f"/api/datasets/{ds}/refreshes", headers=admin).json()
    assert [h["status"] for h in hist] == ["failed", "succeeded"]


def test_invalid_model_and_database_sources(client, published):
    admin, ws = published["admin"], published["ws"]
    r = client.post(
        f"/api/workspaces/{ws}/datasets", json={"definition": {"name": "x", "tables": [{"name": "T"}]}}, headers=admin
    )
    assert r.status_code == 422
    db = {
        "name": "x",
        "tables": [{"name": "T", "source": {"kind": "database", "connection": "postgres:dbname=x", "table": "t"}}],
    }
    assert client.post(f"/api/workspaces/{ws}/datasets", json={"definition": db}, headers=admin).status_code == 400


def test_schedule_and_dashboard(client, published):
    admin, ws, ds = published["admin"], published["ws"], published["ds"]
    assert client.put(f"/api/datasets/{ds}/schedule", json={"cron": "not a cron"}, headers=admin).status_code == 422
    assert (
        client.put(f"/api/datasets/{ds}/schedule", json={"cron": "30 5 * * 1-5"}, headers=admin).json()[
            "refreshSchedule"
        ]
        == "30 5 * * 1-5"
    )
    d = client.post(
        f"/api/workspaces/{ws}/dashboards",
        json={"name": "D", "tiles": [{"reportId": published["report"], "page": "page1", "visual": "v"}]},
        headers=admin,
    ).json()
    got = client.get(f"/api/dashboards/{d['id']}", headers=admin).json()
    assert got["tiles"][0]["datasetId"] == ds


def test_spa_and_static(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/embed/report/1").status_code == 200
    assert client.get("/api/nope").status_code == 404
