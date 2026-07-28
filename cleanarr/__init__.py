"""Public package surface for Cleanarr runtime."""

from importlib.metadata import PackageNotFoundError, version

from . import cleanup
from .cleanup import (
    CONFIG,
    OUTCOME_ABORTED,
    OUTCOME_PARTIAL_FAILURE,
    OUTCOME_SUCCESS,
    CleanupResult,
    MediaCleanup,
)

try:
    __version__ = version("cleanarr")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "CONFIG",
    "CleanupResult",
    "MediaCleanup",
    "OUTCOME_ABORTED",
    "OUTCOME_PARTIAL_FAILURE",
    "OUTCOME_SUCCESS",
    "__version__",
    "cleanup",
]
