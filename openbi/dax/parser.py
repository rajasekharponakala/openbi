"""DAX subset parser built on Lark.

Produces a small AST (dataclasses below) that the compiler turns into
DuckDB SQL. The grammar covers the expression language used by measures,
calculated columns and row-level-security filters: literals, column and
measure references, operators, function calls, ``IN { ... }`` and
``VAR ... RETURN``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from lark import Lark, Token, Transformer, v_args
from lark.exceptions import LarkError

GRAMMAR = r"""
?start: expr

?expr: varblock | or_expr

varblock: (VAR NAME "=" expr)+ RETURN expr

?or_expr: and_expr (OR_OP and_expr)*
?and_expr: cmp_expr (AND_OP cmp_expr)*
?cmp_expr: concat_expr
         | concat_expr CMP_OP concat_expr  -> compare
         | concat_expr IN_KW "{" [expr ("," expr)*] "}" -> in_list
         | NOT_KW cmp_expr -> not_
?concat_expr: add_expr ("&" add_expr)*  -> concat
?add_expr: mul_expr (ADD_OP mul_expr)*
?mul_expr: unary (MUL_OP unary)*
?unary: "-" unary -> neg
      | "+" unary
      | pow_expr
?pow_expr: atom ("^" unary)?
?atom: NUMBER -> number
     | STRING -> string
     | NAME BRACKET -> colref
     | QTABLE BRACKET -> qcolref
     | BRACKET -> bareref
     | QTABLE -> qtable
     | NAME "(" [arg ("," arg)*] ")" -> call
     | NAME -> name
     | "(" expr ")"
?arg: expr

VAR.3: /VAR\b/i
RETURN.3: /RETURN\b/i
IN_KW.3: /IN\b/i
NOT_KW.3: /NOT\b(?!\s*\()/i
OR_OP: "||"
AND_OP: "&&"
CMP_OP: "<=" | ">=" | "<>" | "==" | "=" | "<" | ">"
ADD_OP: "+" | "-"
MUL_OP: "*" | "/"
NAME: /[A-Za-z_][A-Za-z0-9_.]*/
QTABLE: /'(?:[^']|'')+'/
BRACKET: /\[(?:[^\]]|\]\])+\]/
STRING: /"(?:[^"]|"")*"/
NUMBER: /\d+(\.\d*)?([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?/

COMMENT: /--[^\n]*/ | /\/\/[^\n]*/ | /\/\*(.|\n)*?\*\//
%import common.WS
%ignore WS
%ignore COMMENT
"""


class DaxSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    pass


@dataclass(frozen=True)
class Number(Node):
    value: float | int


@dataclass(frozen=True)
class String(Node):
    value: str


@dataclass(frozen=True)
class ColumnRef(Node):
    table: str
    column: str


@dataclass(frozen=True)
class BareRef(Node):
    """``[Name]`` - a measure, or a column of the row-context table."""

    name: str


@dataclass(frozen=True)
class TableRef(Node):
    name: str


@dataclass(frozen=True)
class Name(Node):
    """Unbracketed identifier: a variable or an unquoted table name."""

    name: str


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple[Node, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BinOp(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Neg(Node):
    operand: Node


@dataclass(frozen=True)
class Not(Node):
    operand: Node


@dataclass(frozen=True)
class InList(Node):
    operand: Node
    items: tuple[Node, ...]


@dataclass(frozen=True)
class VarBlock(Node):
    variables: tuple[tuple[str, Node], ...]
    body: Node


def _unbracket(tok: str) -> str:
    return tok[1:-1].replace("]]", "]")


def _unquote_table(tok: str) -> str:
    return tok[1:-1].replace("''", "'")


@v_args(inline=True)
class _ToAst(Transformer):
    def number(self, tok: Token) -> Number:
        s = str(tok)
        return Number(float(s) if any(c in s for c in ".eE") else int(s))

    def string(self, tok: Token) -> String:
        return String(str(tok)[1:-1].replace('""', '"'))

    def colref(self, table: Token, col: Token) -> ColumnRef:
        return ColumnRef(str(table), _unbracket(str(col)))

    def qcolref(self, table: Token, col: Token) -> ColumnRef:
        return ColumnRef(_unquote_table(str(table)), _unbracket(str(col)))

    def bareref(self, tok: Token) -> BareRef:
        return BareRef(_unbracket(str(tok)))

    def qtable(self, tok: Token) -> TableRef:
        return TableRef(_unquote_table(str(tok)))

    def name(self, tok: Token) -> Node:
        n = str(tok)
        if n.upper() in ("TRUE", "FALSE"):
            return Call(n.upper())
        return Name(n)

    def call(self, name: Token, *args: Node | None) -> Call:
        return Call(str(name).upper(), tuple(a for a in args if a is not None))

    def neg(self, operand: Node) -> Neg:
        return Neg(operand)

    def not_(self, _kw: Token, operand: Node) -> Not:
        return Not(operand)

    def compare(self, left: Node, op: Token, right: Node) -> BinOp:
        o = str(op)
        return BinOp("=" if o == "==" else o, left, right)

    def in_list(self, operand: Node, _kw: Token, *items: Node | None) -> InList:
        return InList(operand, tuple(i for i in items if i is not None))

    def concat(self, *parts: Node) -> Node:
        node = parts[0]
        for p in parts[1:]:
            node = BinOp("&", node, p)
        return node

    def _chain(self, *parts: Node | Token) -> Node:
        node = parts[0]
        for i in range(1, len(parts), 2):
            node = BinOp(str(parts[i]), node, parts[i + 1])  # type: ignore[arg-type]
        return node

    add_expr = _chain
    mul_expr = _chain
    or_expr = _chain
    and_expr = _chain

    def pow_expr(self, base: Node, exponent: Node) -> BinOp:
        return BinOp("^", base, exponent)

    def varblock(self, *items: Node | Token) -> VarBlock:
        variables: list[tuple[str, Node]] = []
        i = 0
        while i < len(items) and isinstance(items[i], Token) and items[i].type == "VAR":
            variables.append((str(items[i + 1]), items[i + 2]))  # type: ignore[arg-type]
            i += 3
        body = items[-1]
        return VarBlock(tuple(variables), body)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def _parser() -> Lark:
    return Lark(GRAMMAR, parser="lalr", maybe_placeholders=False)


@lru_cache(maxsize=4096)
def parse(text: str) -> Node:
    text = text.strip()
    if text.startswith("="):
        text = text[1:]
    try:
        tree = _parser().parse(text)
    except LarkError as e:
        raise DaxSyntaxError(f"cannot parse DAX expression {text!r}: {e}") from e
    return _ToAst().transform(tree)
