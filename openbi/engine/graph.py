"""Relationship graph of a semantic model (filter propagation paths)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..model.schema import Relationship, SemanticModel


class UnknownTable(ValueError):
    pass


@dataclass(frozen=True)
class Edge:
    """A join step from ``src`` (many side) to ``dst`` (one side)."""

    src: str
    src_col: str
    dst: str
    dst_col: str


class ModelGraph:
    def __init__(self, model: SemanticModel):
        self.model = model
        self._names = {t.name.lower(): t.name for t in model.tables}
        self._rels: list[Relationship] = [r for r in model.relationships if r.is_active]
        self._reach_cache: dict[str, dict[str, list[Edge]]] = {}

    def canonical(self, table: str) -> str:
        try:
            return self._names[table.lower()]
        except KeyError:
            raise UnknownTable(f"unknown table '{table}'") from None

    def reachable(self, table: str) -> dict[str, list[Edge]]:
        """Tables whose filters propagate to ``table``, with the join path.

        Follows active many-to-one relationships from the many side to the
        one side (the direction a filter on a dimension reaches a fact).
        One-to-one relationships propagate both ways.
        """
        table = self.canonical(table)
        if table in self._reach_cache:
            return self._reach_cache[table]
        paths: dict[str, list[Edge]] = {table: []}
        queue = deque([table])
        while queue:
            cur = queue.popleft()
            for r in self._rels:
                f, t = self.canonical(r.from_table), self.canonical(r.to_table)
                steps = []
                if f == cur:
                    steps.append(Edge(f, r.from_column, t, r.to_column))
                if t == cur and r.cardinality == "oneToOne":
                    steps.append(Edge(t, r.to_column, f, r.from_column))
                for e in steps:
                    if e.dst not in paths:
                        paths[e.dst] = paths[cur] + [e]
                        queue.append(e.dst)
        self._reach_cache[table] = paths
        return paths

    def expanded(self, table: str) -> set[str]:
        """The DAX "expanded table": the table plus everything it can reach."""
        return set(self.reachable(table))

    def bidirectional_into(self, table: str) -> list[Edge]:
        """Relationships with cross-filtering 'both' where ``table`` is the one side.

        Returned edges point from the many side to ``table``; filters on the
        many side (and what it reaches) propagate back to ``table``.
        """
        table = self.canonical(table)
        out = []
        for r in self._rels:
            if r.cross_filter == "both" and self.canonical(r.to_table) == table:
                out.append(Edge(self.canonical(r.from_table), r.from_column, table, r.to_column))
        return out
