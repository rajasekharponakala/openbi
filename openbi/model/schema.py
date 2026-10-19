"""Semantic model definition.

Mirrors the concepts of a Power BI / Analysis Services Tabular model
(as serialised in TMDL / model.bim): tables with a Power Query source,
columns, calculated columns, measures, relationships, and row-level
security roles. Pydantic does the validation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DataType(StrEnum):
    string = "string"
    int64 = "int64"
    double = "double"
    decimal = "decimal"
    boolean = "boolean"
    date = "date"
    datetime = "datetime"


DUCKDB_TYPES = {
    DataType.string: "VARCHAR",
    DataType.int64: "BIGINT",
    DataType.double: "DOUBLE",
    DataType.decimal: "DECIMAL(18,4)",
    DataType.boolean: "BOOLEAN",
    DataType.date: "DATE",
    DataType.datetime: "TIMESTAMP",
}


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Source(_Base):
    """Where a table's data comes from (the first step of a Power Query)."""

    kind: Literal["csv", "parquet", "json", "excel", "sql", "inline", "database"]
    path: str | None = None
    # csv/excel options
    sheet: str | None = None
    delimiter: str | None = None
    header: bool = True
    # sql: a SELECT run by DuckDB (may reference attached databases)
    query: str | None = None
    # inline: literal rows
    rows: list[dict[str, Any]] | None = None
    # database: DuckDB ATTACH connection string, e.g. "postgres:dbname=x host=y"
    connection: str | None = None
    table: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Source:
        need = {
            "csv": "path",
            "parquet": "path",
            "json": "path",
            "excel": "path",
            "sql": "query",
            "inline": "rows",
            "database": "connection",
        }[self.kind]
        if getattr(self, need) is None:
            raise ValueError(f"source kind '{self.kind}' requires '{need}'")
        if self.kind == "database" and not (self.table or self.query):
            raise ValueError("database source requires 'table' or 'query'")
        return self


class Step(_Base):
    """One applied step of a Power Query (M) transformation.

    ``op`` selects the transformation; the remaining fields are its
    arguments (see openbi.powerquery.steps for the catalogue).
    """

    model_config = ConfigDict(extra="allow")
    op: str


class Column(_Base):
    name: str
    data_type: DataType | None = Field(default=None, alias="dataType")
    # DAX row expression for a calculated column (evaluated at refresh)
    expression: str | None = None
    format_string: str | None = Field(default=None, alias="formatString")
    summarize_by: Literal["sum", "average", "min", "max", "count", "distinctCount", "none"] | None = Field(
        default=None, alias="summarizeBy"
    )
    is_hidden: bool = Field(default=False, alias="isHidden")
    sort_by_column: str | None = Field(default=None, alias="sortByColumn")
    description: str | None = None


class Measure(_Base):
    name: str
    expression: str
    format_string: str | None = Field(default=None, alias="formatString")
    description: str | None = None
    display_folder: str | None = Field(default=None, alias="displayFolder")
    is_hidden: bool = Field(default=False, alias="isHidden")


class Table(_Base):
    name: str
    source: Source
    steps: list[Step] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    # import = materialised into the model's DuckDB file (VertiPaq-style)
    # directQuery = a view evaluated against the source on every query
    mode: Literal["import", "directQuery"] = "import"
    is_hidden: bool = Field(default=False, alias="isHidden")
    description: str | None = None

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name.lower() == name.lower():
                return c
        return None


class Relationship(_Base):
    """``from`` is the many side (fact), ``to`` the one side (dimension)."""

    name: str | None = None
    from_table: str = Field(alias="fromTable")
    from_column: str = Field(alias="fromColumn")
    to_table: str = Field(alias="toTable")
    to_column: str = Field(alias="toColumn")
    cardinality: Literal["manyToOne", "oneToOne"] = "manyToOne"
    cross_filter: Literal["single", "both"] = Field(default="single", alias="crossFilteringBehavior")
    is_active: bool = Field(default=True, alias="isActive")


class TablePermission(_Base):
    table: str
    # DAX boolean expression evaluated per row of `table`
    filter_expression: str = Field(alias="filterExpression")


class Role(_Base):
    name: str
    model_permission: Literal["read", "none"] = Field(default="read", alias="modelPermission")
    table_permissions: list[TablePermission] = Field(default_factory=list, alias="tablePermissions")
    members: list[str] = Field(default_factory=list)


class SemanticModel(_Base):
    name: str
    description: str | None = None
    culture: str = "en-US"
    tables: list[Table] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)

    @field_validator("tables")
    @classmethod
    def _unique_tables(cls, v: list[Table]) -> list[Table]:
        names = [t.name.lower() for t in v]
        if len(names) != len(set(names)):
            raise ValueError("table names must be unique")
        return v

    @model_validator(mode="after")
    def _check_refs(self) -> SemanticModel:
        measures = [m.name.lower() for t in self.tables for m in t.measures]
        if len(measures) != len(set(measures)):
            raise ValueError("measure names must be unique across the model")
        for r in self.relationships:
            for t in (r.from_table, r.to_table):
                if self.table(t) is None:
                    raise ValueError(f"relationship references unknown table '{t}'")
        return self

    def table(self, name: str) -> Table | None:
        for t in self.tables:
            if t.name.lower() == name.lower():
                return t
        return None

    def measure(self, name: str) -> tuple[Table, Measure] | None:
        for t in self.tables:
            for m in t.measures:
                if m.name.lower() == name.lower():
                    return t, m
        return None

    def role(self, name: str) -> Role | None:
        for r in self.roles:
            if r.name.lower() == name.lower():
                return r
        return None
