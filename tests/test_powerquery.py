import duckdb
import pytest

from openbi.model.schema import SemanticModel, Step
from openbi.powerquery.refresh import RefreshError, preview_table, refresh_model
from openbi.powerquery.sql import SQLValidationError, expression, select_query
from openbi.powerquery.steps import StepError, compile_steps

ROWS = [
    {"id": 1, "name": " Ann ", "dept": "A", "jan": 10, "feb": 20},
    {"id": 2, "name": "Bob", "dept": "B", "jan": 5, "feb": None},
    {"id": 3, "name": "Cy", "dept": "A", "jan": 7, "feb": 1},
    {"id": 3, "name": "Cy", "dept": "A", "jan": 7, "feb": 1},
]
SRC = "SELECT * FROM (VALUES (1, ' Ann ', 'A', 10, 20), (2, 'Bob', 'B', 5, NULL), (3, 'Cy', 'A', 7, 1), (3, 'Cy', 'A', 7, 1)) AS t(id, name, dept, jan, feb)"


def run(steps, extra=None):
    con = duckdb.connect()
    if extra:
        con.execute(extra)
    sql = compile_steps(SRC, [Step(**s) for s in steps], lambda n: f'"{n}"')
    return con.execute(sql).fetchall()


def test_basic_steps():
    assert run(
        [{"op": "removeDuplicates"}, {"op": "selectColumns", "columns": ["id"]}, {"op": "sort", "by": ["id"]}]
    ) == [(1,), (2,), (3,)]
    assert run(
        [
            {"op": "trim", "columns": ["name"]},
            {"op": "keepTop", "count": 1},
            {"op": "selectColumns", "columns": ["name"]},
        ]
    ) == [("Ann",)]
    assert run(
        [
            {"op": "filterRows", "column": "dept", "operator": "equals", "value": "B"},
            {"op": "selectColumns", "columns": ["name"]},
        ]
    ) == [("Bob",)]
    assert run(
        [
            {"op": "filterRows", "condition": "jan > 6 AND dept = 'A'"},
            {"op": "removeDuplicates", "columns": ["id"]},
            {"op": "sort", "by": ["id"]},
            {"op": "selectColumns", "columns": ["id"]},
        ]
    ) == [(1,), (3,)]
    assert run(
        [
            {"op": "fillNull", "column": "feb", "value": 0},
            {"op": "filterRows", "column": "id", "operator": "equals", "value": 2},
            {"op": "selectColumns", "columns": ["feb"]},
        ]
    ) == [(0,)]


def test_add_rename_change_type():
    rows = run(
        [
            {"op": "addColumn", "name": "total", "expression": "jan + COALESCE(feb, 0)", "dataType": "double"},
            {"op": "renameColumns", "mapping": {"total": "Total"}},
            {"op": "changeType", "types": {"id": "string"}},
            {"op": "selectColumns", "columns": ["id", "Total"]},
            {"op": "sort", "by": [{"column": "Total", "descending": True}]},
            {"op": "keepTop", "count": 1},
        ]
    )
    assert rows == [("1", 30.0)]


def test_group_unpivot_split():
    assert run(
        [
            {
                "op": "groupBy",
                "keys": ["dept"],
                "aggregations": [{"name": "n", "op": "countRows"}, {"name": "s", "op": "sum", "column": "jan"}],
            },
            {"op": "sort", "by": ["dept"]},
        ]
    ) == [("A", 3, 24), ("B", 1, 5)]
    rows = run(
        [
            {"op": "filterRows", "column": "id", "operator": "equals", "value": 1},
            {"op": "selectColumns", "columns": ["id", "jan", "feb"]},
            {"op": "unpivot", "columns": ["jan", "feb"], "attribute": "Month", "value": "Amount"},
        ]
    )
    assert sorted(rows) == [(1, "feb", 20), (1, "jan", 10)]
    rows = run(
        [
            {"op": "filterRows", "column": "id", "operator": "equals", "value": 1},
            {"op": "trim", "columns": ["name"]},
            {"op": "addColumn", "name": "full", "expression": "name || ' Lee'"},
            {"op": "splitColumn", "column": "full", "delimiter": " ", "into": ["First", "Last"]},
            {"op": "selectColumns", "columns": ["First", "Last"]},
        ]
    )
    assert rows == [("Ann", "Lee")]


def test_merge_and_append():
    extra = "CREATE TABLE \"Depts\" AS SELECT * FROM (VALUES ('A', 'Alpha'), ('B', 'Beta')) AS d(code, label)"
    rows = run(
        [
            {"op": "merge", "table": "Depts", "leftOn": "dept", "rightOn": "code", "expand": {"label": "DeptName"}},
            {"op": "removeDuplicates", "columns": ["id"]},
            {"op": "sort", "by": ["id"]},
            {"op": "selectColumns", "columns": ["id", "DeptName"]},
        ],
        extra,
    )
    assert rows == [(1, "Alpha"), (2, "Beta"), (3, "Alpha")]
    extra = 'CREATE TABLE "More" AS SELECT 9 AS id'
    assert run(
        [
            {"op": "append", "tables": ["More"]},
            {"op": "selectColumns", "columns": ["id"]},
            {"op": "sort", "by": [{"column": "id", "descending": True}]},
            {"op": "keepTop", "count": 1},
        ],
        extra,
    ) == [(9,)]


def test_step_errors():
    with pytest.raises(StepError, match="unknown operation"):
        run([{"op": "nope"}])
    with pytest.raises(StepError, match="requires 'columns'"):
        run([{"op": "selectColumns"}])
    with pytest.raises(StepError):
        run([{"op": "filterRows", "condition": "1; DROP TABLE x"}])


def test_sql_validation():
    assert expression("a + 1") == "a + 1"
    with pytest.raises(SQLValidationError):
        expression("SELECT 1")
    with pytest.raises(SQLValidationError):
        select_query("DROP TABLE x")
    with pytest.raises(SQLValidationError):
        select_query("SELECT 1; SELECT 2")
    with pytest.raises(SQLValidationError):
        expression("read_text('/etc/passwd')")


def _model(tables, relationships=()):
    return SemanticModel.model_validate({"name": "m", "tables": tables, "relationships": list(relationships)})


def test_refresh_inline_calculated_column_and_directquery(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "dim.csv").write_text("k,label\n1,one\n2,two\n")
    model = _model(
        [
            {
                "name": "Fact",
                "source": {"kind": "inline", "rows": ROWS},
                "columns": [{"name": "Label", "expression": 'RELATED(Dim[label]) & "!"'}],
            },
            {"name": "Dim", "source": {"kind": "csv", "path": "data/dim.csv"}, "mode": "directQuery"},
        ],
        [{"fromTable": "Fact", "fromColumn": "id", "toTable": "Dim", "toColumn": "k"}],
    )
    res = refresh_model(model, tmp_path / "m.duckdb", base_dir=tmp_path, allowed_root=tmp_path / "data")
    assert {t.name: t.rows for t in res.tables} == {"Fact": 4, "Dim": None}
    con = duckdb.connect(str(tmp_path / "m.duckdb"), read_only=True)
    assert con.execute('SELECT "Label" FROM "Fact" ORDER BY id').fetchall() == [("one!",), ("two!",), ("!",), ("!",)]


def test_refresh_is_sandboxed(tmp_path):
    (tmp_path / "data").mkdir()
    model = _model([{"name": "T", "source": {"kind": "sql", "query": "SELECT * FROM read_csv_auto('/etc/passwd')"}}])
    with pytest.raises(RefreshError):
        refresh_model(model, tmp_path / "m.duckdb", base_dir=tmp_path, allowed_root=tmp_path / "data")
    model = _model([{"name": "T", "source": {"kind": "csv", "path": "../../etc/passwd"}}])
    with pytest.raises(RefreshError, match="outside"):
        refresh_model(model, tmp_path / "m.duckdb", base_dir=tmp_path, allowed_root=tmp_path / "data")
    assert not (tmp_path / "m.duckdb").exists()


def test_preview_upto_step(tmp_path):
    model = _model(
        [
            {
                "name": "T",
                "source": {"kind": "inline", "rows": ROWS},
                "steps": [{"op": "removeDuplicates"}, {"op": "keepTop", "count": 1}],
            }
        ]
    )
    assert len(preview_table(model, "T", tmp_path, upto=0)["rows"]) == 4
    assert len(preview_table(model, "T", tmp_path, upto=1)["rows"]) == 3
    assert len(preview_table(model, "T", tmp_path)["rows"]) == 1
