"""Backwards-compatible shim for the public runtime package."""

from cleanarr_runtime import cleanup as _cleanup

globals().update(_cleanup.__dict__)
