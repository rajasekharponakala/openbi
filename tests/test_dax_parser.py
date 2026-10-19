import pytest

from openbi.dax.parser import (
    BareRef,
    BinOp,
    Call,
    ColumnRef,
    DaxSyntaxError,
    InList,
    Name,
    Not,
    TableRef,
    VarBlock,
    parse,
)


def test_column_and_measure_refs():
    assert parse("Sales[Amount]") == ColumnRef("Sales", "Amount")
    assert parse("'Order Lines'[Net]]Value]") == ColumnRef("Order Lines", "Net]Value")
    assert parse("[Total Sales]") == BareRef("Total Sales")
    assert parse("'Date'") == TableRef("Date")


def test_precedence_and_operators():
    node = parse("1 + 2 * 3")
    assert node == BinOp("+", parse("1"), BinOp("*", parse("2"), parse("3")))
    node = parse('Product[Color] IN {"Red", "Blue"} && NOT Product[Size] = "L"')
    assert isinstance(node, BinOp) and node.op == "&&"
    assert isinstance(node.left, InList)
    assert isinstance(node.right, Not)


def test_functions_and_var_return():
    node = parse("VAR x = SUM(T[a]) VAR y = 2 RETURN x * y")
    assert isinstance(node, VarBlock)
    assert [n for n, _ in node.variables] == ["x", "y"]
    assert node.body == BinOp("*", Name("x"), Name("y"))
    assert parse("calculate([M], all(T))") == Call("CALCULATE", (BareRef("M"), Call("ALL", (Name("T"),))))
    assert parse("TRUE()") == Call("TRUE")
    assert parse("NOT(TRUE())") == Call("NOT", (Call("TRUE"),))


def test_comments_and_leading_equals():
    assert parse("= SUM(T[a]) -- total") == Call("SUM", (ColumnRef("T", "a"),))


def test_syntax_error():
    with pytest.raises(DaxSyntaxError):
        parse("SUM(T[a]")
