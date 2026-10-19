"""Small helpers that delegate SQL quoting/validation to SQLGlot."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

DIALECT = "duckdb"

_FORBIDDEN = (
    exp.Create,
    exp.Drop,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Alter,
    exp.Command,
    exp.Merge,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Pragma,
    exp.Set,
    exp.Use,
)


class SQLValidationError(ValueError):
    pass


def ident(name: str) -> str:
    """Quote an identifier for DuckDB."""
    return exp.to_identifier(name, quoted=True).sql(DIALECT)


def literal(value: Any) -> str:
    """Render a Python value as a DuckDB literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return exp.Literal.number(value).sql(DIALECT)
    return exp.Literal.string(str(value)).sql(DIALECT)


def _check_tree(tree: exp.Expression, text: str) -> None:
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN):
            raise SQLValidationError(f"statement not allowed in expression: {text!r}")
        if isinstance(node, exp.Anonymous) and node.name.lower() in {"read_text", "read_blob", "getenv"}:
            raise SQLValidationError(f"function '{node.name}' is not allowed: {text!r}")


def expression(text: str) -> str:
    """Validate a scalar/boolean SQL expression and return it normalised."""
    try:
        tree = sqlglot.parse_one(text, read=DIALECT)
    except sqlglot.errors.ParseError as e:  # pragma: no cover - message passthrough
        raise SQLValidationError(f"invalid expression {text!r}: {e}") from e
    if isinstance(tree, (exp.Select, exp.Union)) or tree is None:
        raise SQLValidationError(f"expected an expression, got a query: {text!r}")
    _check_tree(tree, text)
    return tree.sql(DIALECT)


def select_query(text: str) -> str:
    """Validate that ``text`` is a single read-only SELECT query."""
    try:
        trees = sqlglot.parse(text, read=DIALECT)
    except sqlglot.errors.ParseError as e:
        raise SQLValidationError(f"invalid query: {e}") from e
    trees = [t for t in trees if t is not None]
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise SQLValidationError("source query must be a single SELECT statement")
    _check_tree(trees[0], text)
    return trees[0].sql(DIALECT)
