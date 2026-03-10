"""Public package surface for Cleanarr runtime."""

from . import cleanup
from .cleanup import CONFIG, MediaCleanup

__version__ = "0.1.0"

__all__ = ["CONFIG", "MediaCleanup", "__version__", "cleanup"]
