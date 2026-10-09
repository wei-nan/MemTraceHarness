"""Services that sit in front of a Harness: transports and presentation.

A service never imports `memtrace_harness`, never reads a Harness's database or env file, and holds
only its own secrets (a bot token, say). Its one way to a Harness is that Harness's local HTTP API
(`HarnessClient`). `tests/test_service_boundary.py` keeps it that way.
"""
