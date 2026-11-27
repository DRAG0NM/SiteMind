"""B05 source adapters; all times exposed through B01 records."""
from .hipe import HipeAdapter
from .pvgis import PvgisAdapter

__all__ = ["HipeAdapter", "PvgisAdapter"]
