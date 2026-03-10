"""Public package surface for Cleanarr runtime."""

from importlib.metadata import PackageNotFoundError, version

from . import cleanup
from .cleanup import CONFIG, MediaCleanup

try:
    __version__ = version("cleanarr-runtime")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["CONFIG", "MediaCleanup", "__version__", "cleanup"]
