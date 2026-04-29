"""Shared Hypothesis configuration for unit tests.

Sets deadline=None to avoid flaky DeadlineExceeded errors caused by
cold module imports on the first Hypothesis example.
"""

from hypothesis import settings

settings.register_profile("ci", deadline=None)
settings.load_profile("ci")
