from pathlib import Path

import duckdb
import pytest

from openbi.model.project import load_project
from openbi.powerquery.refresh import open_model_db, refresh_model
from openbi.samples import create_sample_project


@pytest.fixture(scope="session")
def sample(tmp_path_factory):
    root = tmp_path_factory.mktemp("sample")
    create_sample_project(root, orders=1500)
    project = load_project(root)
    db = root / ".openbi" / "model.duckdb"
    refresh_model(project.model, db, base_dir=root)
    return project


@pytest.fixture(scope="session")
def con(sample) -> duckdb.DuckDBPyConnection:
    return open_model_db(Path(sample.root) / ".openbi" / "model.duckdb")
