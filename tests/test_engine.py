"""Engine results are checked against hand-written SQL over the same tables."""

from datetime import date

import duckdb
import pytest

from openbi.engine.query import QueryEngine, QueryError, SemanticQuery, parse_select
from openbi.model.schema import SemanticModel
from openbi.powerquery.refresh import refresh_model

LINE = 'SUM(s."Line Amount")'


def run(sample, con, select, filters=(), roles=None, user=None, order=(), top=None, today=None):
    engine = QueryEngine(sample.model, roles=roles, user=user, today=today)
    res = engine.execute(con, SemanticQuery(parse_select(list(select)), list(filters), list(order), top))
    return res.rows


def sql(con, q):
    return con.execute(q).fetchall()


def approx_rows(a, b):
    assert len(a) == len(b), (a, b)
    for ra, rb in zip(a, b, strict=True):
        assert len(ra) == len(rb)
        for x, y in zip(ra, rb, strict=True):
            if isinstance(x, float) or isinstance(y, float):
                assert x == pytest.approx(y, rel=1e-9), (ra, rb)
            else:
                assert x == y, (ra, rb)


def test_grand_totals(sample, con):
    rows = run(sample, con, ["[Total Sales]", "[Units]", "[Orders]", "[Total Cost]"])
    expected = sql(
        con,
        f'''SELECT {LINE}, SUM(s."Quantity"), COUNT(DISTINCT s."OrderID"), SUM(s."Quantity" * p."Cost")
        FROM "Sales" s LEFT JOIN "Product" p ON s."ProductKey" = p."ProductKey"''',
    )
    approx_rows(rows, expected)


def test_group_by_dimension_through_snowflake(sample, con):
    rows = run(sample, con, ["Region[Region]", "[Total Sales]", "[Profit Margin]"])
    expected = sql(
        con,
        f"""SELECT r."Region", {LINE}, ({LINE} - SUM(s."Quantity" * p."Cost")) / {LINE}
        FROM "Sales" s JOIN "Customer" c USING ("CustomerKey") JOIN "Region" r ON c."Region" = r."Region"
        JOIN "Product" p ON s."ProductKey" = p."ProductKey" GROUP BY 1 ORDER BY 1""",
    )
    approx_rows(rows, expected)


def test_two_fact_tables_share_dimensions(sample, con):
    rows = run(sample, con, ["Region[Region]", "Date[Year]", "[Total Sales]", "[Target]"])
    sales = dict(
        ((r, y), v)
        for r, y, v in sql(
            con,
            f"""SELECT c."Region", year(s."OrderDate"), {LINE} FROM "Sales" s
        JOIN "Customer" c USING ("CustomerKey") GROUP BY 1, 2""",
        )
    )
    target = dict(
        ((r, y), v)
        for r, y, v in sql(con, """SELECT "Region", year("MonthStart"), SUM("Target") FROM "Targets" GROUP BY 1, 2""")
    )
    assert len(rows) == len(set(sales) | set(target))
    for region, year, s, t in rows:
        assert s == pytest.approx(sales.get((region, year)))
        assert t == pytest.approx(target.get((region, year)))


def test_calculate_all_and_boolean_filter_semantics(sample, con):
    rows = run(sample, con, ["Product[Category]", "[Total Sales]", "[% of All Sales]", "[Bike Sales]"])
    total = sql(con, f'SELECT {LINE} FROM "Sales" s')[0][0]
    bikes = sql(
        con, f"""SELECT {LINE} FROM "Sales" s JOIN "Product" p USING ("ProductKey") WHERE p."Category" = 'Bikes' """
    )[0][0]
    by_cat = dict(
        sql(con, f'SELECT p."Category", {LINE} FROM "Sales" s JOIN "Product" p USING ("ProductKey") GROUP BY 1')
    )
    for cat, s, pct, bike in rows:
        assert s == pytest.approx(by_cat[cat])
        assert pct == pytest.approx(by_cat[cat] / total)
        # CALCULATE's boolean filter replaces the Category filter coming from the row
        assert bike == pytest.approx(bikes)
    # ... but filters on other columns are kept
    rows = run(sample, con, ["Date[Year]", "[Bike Sales]"])
    by_year = dict(
        sql(
            con,
            f"""SELECT year(s."OrderDate"), {LINE} FROM "Sales" s JOIN "Product" p USING ("ProductKey")
        WHERE p."Category" = 'Bikes' GROUP BY 1""",
        )
    )
    assert {y: pytest.approx(v) for y, v in by_year.items()} == dict(rows)


def test_all_respects_outer_filters_on_other_tables(sample, con):
    # ALL(Sales) removes filters on the expanded Sales table (incl. Date), so the
    # denominator is the grand total even when the visual is filtered by year.
    f = [{"filterType": 1, "target": {"table": "Date", "column": "Year"}, "operator": "In", "values": [2024]}]
    rows = run(sample, con, ["[Total Sales]", "[% of All Sales]"], filters=f)
    total = sql(con, f'SELECT {LINE} FROM "Sales" s')[0][0]
    y2024 = sql(con, f'SELECT {LINE} FROM "Sales" s WHERE year(s."OrderDate") = 2024')[0][0]
    approx_rows(rows, [(y2024, y2024 / total)])


def test_countrows_filter_iterator_and_blank(sample, con):
    rows = run(sample, con, ["[Large Orders]"])
    assert rows[0][0] == sql(con, 'SELECT COUNT(*) FROM "Sales" WHERE "Quantity" >= 3')[0][0]
    f = [
        {
            "filterType": 1,
            "target": {"table": "Product", "column": "Category"},
            "operator": "In",
            "values": ["__none__"],
        }
    ]
    assert run(sample, con, ["[Large Orders]", "[Total Sales]"], filters=f) == [[None, None]]


def test_sort_by_column(sample, con):
    rows = run(sample, con, ["Date[Month]", "[Units]"])
    assert [r[0] for r in rows] == ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def test_filters(sample, con):
    adv = {
        "filterType": 0,
        "target": {"table": "Product", "column": "List Price"},
        "logicalOperator": "And",
        "conditions": [{"operator": "GreaterThanOrEqual", "value": 500}],
    }
    rows = run(sample, con, ["[Units]"], filters=[adv])
    assert (
        rows[0][0]
        == sql(
            con, 'SELECT SUM("Quantity") FROM "Sales" JOIN "Product" USING ("ProductKey") WHERE "List Price" >= 500'
        )[0][0]
    )
    notin = {
        "filterType": 1,
        "target": {"table": "Region", "column": "Region"},
        "operator": "NotIn",
        "values": ["West", "East"],
    }
    rows = run(sample, con, ["Region[Region]"], filters=[notin])
    assert rows == [["Central"], ["South"]]
    contains = {
        "filterType": 0,
        "target": {"table": "Product", "column": "Subcategory"},
        "logicalOperator": "Or",
        "conditions": [{"operator": "Contains", "value": "bike"}, {"operator": "StartsWith", "value": "Hel"}],
    }
    rows = run(sample, con, ["Product[Subcategory]"], filters=[contains])
    assert [r[0] for r in rows] == ["Helmets", "Mountain Bikes", "Road Bikes", "Touring Bikes"]


def test_measure_filter_and_topn(sample, con):
    mf = {
        "filterType": 0,
        "target": {"table": "Sales", "measure": "Total Sales"},
        "logicalOperator": "And",
        "conditions": [{"operator": "GreaterThan", "value": 100000}],
    }
    rows = run(sample, con, ["Product[Subcategory]", "[Units]"], filters=[mf])
    expected = sql(
        con,
        f"""SELECT p."Subcategory", SUM(s."Quantity") FROM "Sales" s JOIN "Product" p USING ("ProductKey")
        GROUP BY 1 HAVING {LINE} > 100000 ORDER BY 1""",
    )
    approx_rows(rows, expected)
    topn = {
        "filterType": 5,
        "target": {"table": "Customer", "column": "Customer"},
        "operator": "Top",
        "itemCount": 3,
        "orderBy": {"table": "Sales", "measure": "Total Sales"},
    }
    rows = run(sample, con, ["Customer[Customer]", "[Total Sales]"], filters=[topn], order=[("Total Sales", True)])
    expected = sql(
        con,
        f"""SELECT c."Customer", {LINE} FROM "Sales" s JOIN "Customer" c USING ("CustomerKey")
        GROUP BY 1 ORDER BY 2 DESC LIMIT 3""",
    )
    approx_rows(rows, expected)


def test_relative_date_filter(sample, con):
    f = {
        "filterType": 4,
        "target": {"table": "Date", "column": "Date"},
        "operator": 0,
        "timeUnitsCount": 3,
        "timeUnitType": 4,
        "includeToday": True,
    }  # last 3 calendar months
    rows = run(sample, con, ["[Units]"], filters=[f], today=date(2025, 7, 15))
    expected = sql(
        con,
        """SELECT SUM("Quantity") FROM "Sales" WHERE "OrderDate" >= DATE '2025-04-01' AND "OrderDate" < DATE '2025-07-01' """,
    )
    assert rows[0][0] == expected[0][0]


def test_row_level_security(sample, con):
    rows = run(
        sample, con, ["Region[Region]", "[Total Sales]", "[Target]"], roles=["Regional Manager"], user="sam@example.com"
    )
    assert [r[0] for r in rows] == ["South"]
    # RLS propagates to facts even when the dimension isn't on the visual, and ALL() cannot remove it
    rows = run(sample, con, ["[Total Sales]", "[% of All Sales]"], roles=["West Only"])
    west = sql(
        con, f"""SELECT {LINE} FROM "Sales" s JOIN "Customer" c USING ("CustomerKey") WHERE c."Region" = 'West' """
    )[0][0]
    approx_rows(rows, [(west, 1.0)])
    # union of roles
    rows = run(sample, con, ["Region[Region]"], roles=["West Only", "Regional Manager"], user="sam@example.com")
    assert [r[0] for r in rows] == ["South", "West"]
    assert run(sample, con, ["Region[Region]"], roles=["Regional Manager"], user="nobody@example.com") == []


def test_slicer_query_without_measures(sample, con):
    rows = run(sample, con, ["Product[Category]"])
    assert [r[0] for r in rows] == ["Accessories", "Bikes", "Clothing", "Components"]
    # unrelated filters don't reach a dimension-only query (single-direction relationships)
    f = [{"filterType": 1, "target": {"table": "Region", "column": "Region"}, "operator": "In", "values": ["West"]}]
    assert len(run(sample, con, ["Product[Category]"], filters=f)) == 4


def test_implicit_measures_and_adhoc_expressions(sample, con):
    rows = run(
        sample,
        con,
        [
            {"column": "Sales[Quantity]", "aggregate": "sum"},
            {"expression": "AVERAGEX(Sales, Sales[Quantity])", "name": "avg"},
        ],
    )
    expected = sql(con, 'SELECT SUM("Quantity"), AVG("Quantity") FROM "Sales"')
    approx_rows(rows, expected)


def test_select_order_is_preserved(sample, con):
    engine = QueryEngine(sample.model)
    res = engine.execute(con, SemanticQuery(parse_select(["[Units]", "Product[Category]"]), [], [], None))
    assert [c["name"] for c in res.columns] == ["Units", "Product[Category]"]


def test_errors(sample, con):
    with pytest.raises(QueryError, match="unknown measure"):
        run(sample, con, ["[Nope]"])
    with pytest.raises(QueryError, match="without an aggregation"):
        run(sample, con, [{"expression": "Sales[Quantity]", "name": "x"}])
    with pytest.raises(QueryError, match="unknown table"):
        run(sample, con, ["Nope[x]"])


def test_bidirectional_and_multi_role_model(tmp_path):
    model = SemanticModel.model_validate(
        {
            "name": "bidi",
            "tables": [
                {
                    "name": "Fact",
                    "source": {
                        "kind": "inline",
                        "rows": [
                            {"cust": 1, "prod": "a", "amt": 10},
                            {"cust": 1, "prod": "b", "amt": 5},
                            {"cust": 2, "prod": "b", "amt": 7},
                        ],
                    },
                    "measures": [{"name": "Amount", "expression": "SUM(Fact[amt])"}],
                },
                {
                    "name": "Cust",
                    "source": {
                        "kind": "inline",
                        "rows": [{"id": 1, "name": "Ann"}, {"id": 2, "name": "Bob"}, {"id": 3, "name": "Cy"}],
                    },
                    "measures": [{"name": "Customers", "expression": "COUNTROWS(Cust)"}],
                },
                {"name": "Prod", "source": {"kind": "inline", "rows": [{"code": "a"}, {"code": "b"}]}},
            ],
            "relationships": [
                {
                    "fromTable": "Fact",
                    "fromColumn": "cust",
                    "toTable": "Cust",
                    "toColumn": "id",
                    "crossFilteringBehavior": "both",
                },
                {"fromTable": "Fact", "fromColumn": "prod", "toTable": "Prod", "toColumn": "code"},
            ],
        }
    )
    refresh_model(model, tmp_path / "m.duckdb", base_dir=tmp_path)
    con = duckdb.connect(str(tmp_path / "m.duckdb"), read_only=True)
    engine = QueryEngine(model)
    q = SemanticQuery(parse_select(["Prod[code]", "[Customers]", "[Amount]"]))
    # Prod filters Fact, which (bidirectionally) filters Cust
    assert engine.execute(con, q).rows == [["a", 1, 10], ["b", 2, 12]]
    q = SemanticQuery(parse_select(["[Customers]"]))
    assert engine.execute(con, q).rows == [[3]]
