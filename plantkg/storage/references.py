"""B04 can inject fixed-manifest lookup without publishing versions here."""
from typing import Protocol, Sequence

from plantkg.core.contracts import Context


class ReferenceResolver(Protocol):
    def graph(self, context: Context, graph_name: str) -> str | None: ...
    def series(self, context: Context, quantity: str) -> Sequence[str]: ...


class DenyReferences:
    def graph(self, context: Context, graph_name: str) -> None:
        return None

    def series(self, context: Context, quantity: str) -> tuple[str, ...]:
        return ()
