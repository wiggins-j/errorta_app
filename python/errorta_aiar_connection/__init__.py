"""F116 - canonical AIAR connection authority.

This package owns the product-level answer to "which AIAR is Errorta using?".
It intentionally sits above the older remote-AIAR and data-residency helpers so
feature code can resolve one active runtime instead of re-reading several
partially-overlapping configs.
"""

from .models import AiarCapabilities, AiarRuntime
from .resolver import resolve_aiar_runtime

__all__ = ["AiarCapabilities", "AiarRuntime", "resolve_aiar_runtime"]
