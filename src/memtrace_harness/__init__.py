"""MemTrace Harness package."""

import resource

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 4096:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
except Exception:
    pass

__all__ = ["__version__"]

__version__ = "0.4.0"
