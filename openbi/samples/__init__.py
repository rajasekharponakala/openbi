"""Sample "Contoso-style" sales project used by the demo and the tests."""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

from ..model.project import Project, save_project
from ..model.schema import SemanticModel

REGIONS = [
    ("West", "Avery Chen", "avery@example.com"),
    ("East", "Jordan Patel", "jordan@example.com"),
    ("Central", "Riley Okafor", "riley@example.com"),
    ("South", "Sam Lindqvist", "sam@example.com"),
]
CITIES = {
    "West": ["Seattle", "Portland", "San Francisco", "Los Angeles"],
    "East": ["New York", "Boston", "Philadelphia"],
    "Central": ["Chicago", "Denver", "Minneapolis"],
    "South": ["Austin", "Atlanta", "Miami"],
}
PRODUCTS = {
    "Bikes": {"Mountain Bikes": (1200, 680), "Road Bikes": (1500, 820), "Touring Bikes": (980, 560)},
    "Components": {"Brakes": (95, 41), "Chains": (35, 14), "Wheels": (240, 120), "Pedals": (60, 25)},
    "Clothing": {"Jerseys": (55, 21), "Gloves": (25, 9), "Shorts": (45, 17)},
    "Accessories": {"Helmets": (70, 28), "Bottles": (9, 3), "Lights": (32, 12), "Locks": (28, 11)},
}
COLORS = ["Black", "Red", "Silver", "Blue", "Yellow"]
FIRST = ["Alex", "Blake", "Casey", "Drew", "Emery", "Finley", "Gray", "Harper", "Indy", "Jules", "Kai", "Lane"]
LAST = ["Garcia", "Kim", "Nguyen", "Smith", "Rossi", "Müller", "Silva", "Haddad", "Ito", "Brown"]


def _write(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def generate_data(data_dir: Path, seed: int = 42, orders: int = 4000) -> None:
    rnd = random.Random(seed)
    data_dir.mkdir(parents=True, exist_ok=True)
    _write(data_dir / "regions.csv", ["Region", "Manager", "ManagerEmail"], [list(r) for r in REGIONS])

    products = []
    key = 1
    for cat, subs in PRODUCTS.items():
        for sub, (price, cost) in subs.items():
            for i in range(3):
                color = rnd.choice(COLORS)
                p = round(price * (0.85 + 0.15 * i), 2)
                c = round(cost * (0.85 + 0.15 * i), 2)
                # leading/trailing spaces are cleaned up by a Power Query step
                products.append(
                    [key, f" {sub[:-1] if sub.endswith('s') else sub} {100 + i * 100} ", cat, sub, color, p, c]
                )
                key += 1
    _write(
        data_dir / "products.csv",
        ["ProductKey", "Product", "Category", "Subcategory", "Color", "ListPrice", "Cost"],
        products,
    )

    customers = []
    names: set[str] = set()
    for ck in range(1, 301):
        region = rnd.choice(REGIONS)[0]
        name = f"{rnd.choice(FIRST)} {chr(65 + rnd.randrange(26))}. {rnd.choice(LAST)}"
        while name in names:
            name = f"{rnd.choice(FIRST)} {chr(65 + rnd.randrange(26))}. {rnd.choice(LAST)}"
        names.add(name)
        customers.append(
            [ck, name, rnd.choice(CITIES[region]), region, rnd.choice(["Consumer", "Corporate", "Small Business"])]
        )
    _write(data_dir / "customers.csv", ["CustomerKey", "Customer", "City", "Region", "Segment"], customers)

    start = date(2023, 1, 1)
    days = (date(2025, 12, 31) - start).days
    sales = []
    line = 1
    for order in range(1, orders + 1):
        # gentle growth + seasonality
        d = start + timedelta(days=int(days * (rnd.random() ** 0.8)))
        cust = rnd.randint(1, len(customers))
        for _ in range(rnd.choice([1, 1, 1, 2, 2, 3])):
            prod = rnd.choice(products)
            qty = rnd.choice([1, 1, 1, 2, 2, 3, 4])
            if rnd.random() < 0.01:
                qty = -qty  # returns, removed by a filter step
            discount = rnd.choice([0, 0, 0, 0.05, 0.1, 0.15])
            sales.append([line, f"SO{order:05d}", d.isoformat(), prod[0], cust, qty, prod[5], discount])
            line += 1
    _write(
        data_dir / "sales.csv",
        ["SalesLineKey", "OrderID", "OrderDate", "ProductKey", "CustomerKey", "Quantity", "UnitPrice", "Discount"],
        sales,
    )

    targets = []
    for region, _, _ in REGIONS:
        base = rnd.randint(22, 30) * 1000
        for y in (2023, 2024, 2025):
            for m in range(1, 13):
                targets.append(
                    [
                        region,
                        date(y, m, 1).isoformat(),
                        round(base * (1 + 0.12 * (y - 2023)) * (0.9 + 0.2 * rnd.random())),
                    ]
                )
    _write(data_dir / "targets.csv", ["Region", "MonthStart", "Target"], targets)


MODEL = {
    "name": "Sales",
    "description": "Bike retailer sales - a star schema with two fact tables",
    "tables": [
        {
            "name": "Sales",
            "source": {"kind": "csv", "path": "data/sales.csv"},
            "steps": [
                {
                    "op": "changeType",
                    "types": {"OrderDate": "date", "Quantity": "int64", "UnitPrice": "double", "Discount": "double"},
                },
                {"op": "filterRows", "column": "Quantity", "operator": "greaterThan", "value": 0},
                {"op": "removeColumns", "columns": ["SalesLineKey"]},
            ],
            "columns": [
                {
                    "name": "Line Amount",
                    "dataType": "double",
                    "expression": "Sales[Quantity] * Sales[UnitPrice] * (1 - Sales[Discount])",
                },
            ],
            "measures": [
                {"name": "Total Sales", "expression": "SUM(Sales[Line Amount])", "formatString": "$#,0"},
                {
                    "name": "Total Cost",
                    "expression": "SUMX(Sales, Sales[Quantity] * RELATED(Product[Cost]))",
                    "formatString": "$#,0",
                },
                {"name": "Profit", "expression": "[Total Sales] - [Total Cost]", "formatString": "$#,0"},
                {"name": "Profit Margin", "expression": "DIVIDE([Profit], [Total Sales])", "formatString": "0.0%"},
                {"name": "Orders", "expression": "DISTINCTCOUNT(Sales[OrderID])", "formatString": "#,0"},
                {"name": "Units", "expression": "SUM(Sales[Quantity])", "formatString": "#,0"},
                {"name": "Avg Order Value", "expression": "DIVIDE([Total Sales], [Orders])", "formatString": "$#,0.00"},
                {
                    "name": "% of All Sales",
                    "expression": "DIVIDE([Total Sales], CALCULATE([Total Sales], ALL(Sales)))",
                    "formatString": "0.0%",
                },
                {
                    "name": "Bike Sales",
                    "expression": "CALCULATE([Total Sales], 'Product'[Category] = \"Bikes\")",
                    "formatString": "$#,0",
                },
                {
                    "name": "Large Orders",
                    "expression": "COUNTROWS(FILTER(Sales, Sales[Quantity] >= 3))",
                    "formatString": "#,0",
                },
            ],
        },
        {
            "name": "Targets",
            "source": {"kind": "csv", "path": "data/targets.csv"},
            "steps": [{"op": "changeType", "types": {"MonthStart": "date", "Target": "double"}}],
            "measures": [
                {"name": "Target", "expression": "SUM(Targets[Target])", "formatString": "$#,0"},
                {"name": "Target Attainment", "expression": "DIVIDE([Total Sales], [Target])", "formatString": "0.0%"},
                {"name": "Variance to Target", "expression": "[Total Sales] - [Target]", "formatString": "$#,0"},
            ],
        },
        {
            "name": "Product",
            "source": {"kind": "csv", "path": "data/products.csv"},
            "steps": [
                {"op": "trim", "columns": ["Product"]},
                {"op": "renameColumns", "mapping": {"ListPrice": "List Price"}},
                {
                    "op": "addColumn",
                    "name": "Price Band",
                    "dataType": "string",
                    "expression": "CASE WHEN \"List Price\" >= 500 THEN 'Premium' WHEN \"List Price\" >= 50 THEN 'Mid' ELSE 'Value' END",
                },
            ],
            "columns": [{"name": "ProductKey", "isHidden": True}],
            "measures": [{"name": "Product Count", "expression": "COUNTROWS(Product)", "formatString": "#,0"}],
        },
        {
            "name": "Customer",
            "source": {"kind": "csv", "path": "data/customers.csv"},
            "columns": [{"name": "CustomerKey", "isHidden": True}],
            "measures": [
                {"name": "Customers", "expression": "DISTINCTCOUNT(Sales[CustomerKey])", "formatString": "#,0"}
            ],
        },
        {
            "name": "Region",
            "source": {"kind": "csv", "path": "data/regions.csv"},
        },
        {
            "name": "Date",
            "source": {
                "kind": "sql",
                "query": "SELECT CAST(range AS DATE) AS \"Date\" FROM range(DATE '2023-01-01', DATE '2026-01-01', INTERVAL 1 DAY)",
            },
            "steps": [
                {"op": "addColumn", "name": "Year", "expression": 'year("Date")', "dataType": "int64"},
                {"op": "addColumn", "name": "Quarter", "expression": "'Q' || quarter(\"Date\")"},
                {"op": "addColumn", "name": "MonthNumber", "expression": 'month("Date")', "dataType": "int64"},
                {"op": "addColumn", "name": "Month", "expression": "strftime(\"Date\", '%b')"},
                {"op": "addColumn", "name": "Year-Month", "expression": "strftime(\"Date\", '%Y-%m')"},
            ],
            "columns": [
                {"name": "Month", "sortByColumn": "MonthNumber"},
                {"name": "MonthNumber", "isHidden": True},
            ],
        },
    ],
    "relationships": [
        {"fromTable": "Sales", "fromColumn": "ProductKey", "toTable": "Product", "toColumn": "ProductKey"},
        {"fromTable": "Sales", "fromColumn": "CustomerKey", "toTable": "Customer", "toColumn": "CustomerKey"},
        {"fromTable": "Sales", "fromColumn": "OrderDate", "toTable": "Date", "toColumn": "Date"},
        {"fromTable": "Customer", "fromColumn": "Region", "toTable": "Region", "toColumn": "Region"},
        {"fromTable": "Targets", "fromColumn": "Region", "toTable": "Region", "toColumn": "Region"},
        {"fromTable": "Targets", "fromColumn": "MonthStart", "toTable": "Date", "toColumn": "Date"},
    ],
    "roles": [
        {
            "name": "Regional Manager",
            "tablePermissions": [{"table": "Region", "filterExpression": "Region[ManagerEmail] = USERPRINCIPALNAME()"}],
        },
        {"name": "West Only", "tablePermissions": [{"table": "Region", "filterExpression": 'Region[Region] = "West"'}]},
    ],
}


def _v(name, type_, x, y, w, h, fields, title=None, **fmt):
    v = {"name": name, "type": type_, "x": x, "y": y, "w": w, "h": h, "fields": fields}
    if title:
        v["title"] = title
    if fmt:
        v["format"] = fmt
    return v


REPORT = {
    "name": "Sales Overview",
    "filters": [],
    "pages": [
        {
            "name": "overview",
            "displayName": "Overview",
            "filters": [],
            "visuals": [
                _v("slicerYear", "slicer", 0, 0, 2, 2, {"field": ["Date[Year]"]}, "Year", style="dropdown"),
                _v("slicerRegion", "slicer", 2, 0, 2, 2, {"field": ["Region[Region]"]}, "Region", style="dropdown"),
                _v("cardSales", "card", 4, 0, 2, 2, {"values": ["[Total Sales]"]}),
                _v("cardProfit", "card", 6, 0, 2, 2, {"values": ["[Profit]"]}),
                _v("cardMargin", "card", 8, 0, 2, 2, {"values": ["[Profit Margin]"]}),
                _v("cardOrders", "card", 10, 0, 2, 2, {"values": ["[Orders]"]}),
                _v(
                    "salesByMonth",
                    "comboChart",
                    0,
                    2,
                    8,
                    4,
                    {"category": ["Date[Year-Month]"], "values": ["[Total Sales]"], "lineValues": ["[Target]"]},
                    "Sales vs target by month",
                ),
                _v(
                    "salesByCategory",
                    "donutChart",
                    8,
                    2,
                    4,
                    4,
                    {"category": ["Product[Category]"], "values": ["[Total Sales]"]},
                    "Sales by category",
                ),
                _v(
                    "salesBySub",
                    "barChart",
                    0,
                    6,
                    4,
                    5,
                    {"category": ["Product[Subcategory]"], "values": ["[Total Sales]"]},
                    "Top subcategories",
                    sort="valueDesc",
                ),
                _v(
                    "regionTable",
                    "table",
                    4,
                    6,
                    4,
                    5,
                    {
                        "columns": [
                            "Region[Region]",
                            "[Total Sales]",
                            "[Target]",
                            "[Target Attainment]",
                            "[Profit Margin]",
                        ]
                    },
                    "Regional performance",
                ),
                _v(
                    "segmentMonth",
                    "stackedColumnChart",
                    8,
                    6,
                    4,
                    5,
                    {"category": ["Date[Quarter]"], "series": ["Customer[Segment]"], "values": ["[Total Sales]"]},
                    "Sales by quarter and segment",
                ),
            ],
        },
        {
            "name": "products",
            "displayName": "Products",
            "filters": [],
            "visuals": [
                _v("slicerCat", "slicer", 0, 0, 3, 5, {"field": ["Product[Category]"]}, "Category", style="list"),
                _v(
                    "matrix",
                    "matrix",
                    3,
                    0,
                    9,
                    5,
                    {"rows": ["Product[Subcategory]"], "columns": ["Date[Year]"], "values": ["[Total Sales]"]},
                    "Subcategory by year",
                ),
                _v(
                    "scatter",
                    "scatterChart",
                    0,
                    5,
                    6,
                    5,
                    {
                        "category": ["Product[Product]"],
                        "x": ["[Units]"],
                        "y": ["[Profit Margin]"],
                        "size": ["[Total Sales]"],
                    },
                    "Units vs margin",
                ),
                _v(
                    "treemap",
                    "treemap",
                    6,
                    5,
                    6,
                    5,
                    {"category": ["Product[Subcategory]"], "values": ["[Profit]"]},
                    "Profit by subcategory",
                ),
            ],
        },
        {
            "name": "customers",
            "displayName": "Customers",
            "filters": [
                {
                    "$schema": "http://powerbi.com/product/schema#basic",
                    "filterType": 1,
                    "target": {"table": "Date", "column": "Year"},
                    "operator": "In",
                    "values": [2025],
                }
            ],
            "visuals": [
                _v(
                    "topCustomers",
                    "table",
                    0,
                    0,
                    6,
                    6,
                    {"columns": ["Customer[Customer]", "Customer[City]", "[Total Sales]", "[Orders]"]},
                    "Top 10 customers (2025)",
                    sortBy="Total Sales",
                    sortDir="desc",
                ),
                _v(
                    "cityBar",
                    "columnChart",
                    6,
                    0,
                    6,
                    3,
                    {"category": ["Customer[City]"], "values": ["[Customers]"]},
                    "Active customers by city",
                    sort="valueDesc",
                ),
                _v(
                    "gaugeTarget",
                    "gauge",
                    6,
                    3,
                    3,
                    3,
                    {"values": ["[Total Sales]"], "target": ["[Target]"]},
                    "Attainment",
                ),
                _v(
                    "funnel",
                    "funnel",
                    9,
                    3,
                    3,
                    3,
                    {"category": ["Customer[Segment]"], "values": ["[Orders]"]},
                    "Orders by segment",
                ),
            ],
        },
    ],
}
# visual-level TopN filter, as the Power BI filter pane would author it
REPORT["pages"][2]["visuals"][0]["filters"] = [
    {
        "$schema": "http://powerbi.com/product/schema#topN",
        "filterType": 5,
        "target": {"table": "Customer", "column": "Customer"},
        "operator": "Top",
        "itemCount": 10,
        "orderBy": {"table": "Sales", "measure": "Total Sales"},
    }
]


def create_sample_project(root: str | Path, seed: int = 42, orders: int = 4000) -> Project:
    root = Path(root)
    generate_data(root / "data", seed=seed, orders=orders)
    project = Project(root, "Sales Sample", SemanticModel.model_validate(MODEL), {"sales-overview": REPORT})
    save_project(project)
    return project
