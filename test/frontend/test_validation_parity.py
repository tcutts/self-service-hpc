"""Unit tests for frontend/backend POSIX username validation parity.

Verifies that the JavaScript regex from frontend/js/app.js and the
Python validator from lambda/shared/validators.py agree on a shared
set of test vectors.  Since the JS regex /^[a-z_][a-z0-9_-]{0,31}$/
and the Python regex ^[a-z_][a-z0-9_-]{0,31}$ are identical, we test
parity by running both the compiled regex and the Python validator
function against every test vector and asserting they agree.

Validates: Requirements 2.4, 3.1
"""

import os
import re
import sys

import pytest

# Make lambda/shared importable so we can use validate_posix_username.
_LAMBDA_SHARED = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambda", "shared",
)
sys.path.insert(0, _LAMBDA_SHARED)

from validators import validate_posix_username  # noqa: E402

# The JavaScript regex from frontend/js/app.js, replicated in Python.
JS_POSIX_USERNAME_REGEX = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Shared test vectors: (username, expected_valid)
TEST_VECTORS = [
    # --- Valid usernames ---
    ("a", True),
    ("jsmith", True),
    ("_admin", True),
    ("dev-user-01", True),
    ("a" * 32, True),
    ("_", True),
    ("a123", True),
    ("a---", True),
    ("z0_-", True),
    # --- Invalid usernames ---
    ("", False),
    ("A", False),
    ("1user", False),
    ("-user", False),
    ("user@corp", False),
    ("user.name", False),
    ("user name", False),
    ("a" * 33, False),
    ("Admin", False),
    ("USER", False),
    ("root@localhost", False),
    ("hello world", False),
]


class TestValidationParity:
    """Verify the JS regex and Python validator agree on all test vectors."""

    @pytest.mark.parametrize("username, expected_valid", TEST_VECTORS)
    def test_python_validator_matches_expected(self, username, expected_valid):
        """The Python validate_posix_username function matches the expected result."""
        is_valid, error_msg = validate_posix_username(username)
        assert is_valid == expected_valid, (
            f"Python validator: expected {expected_valid} for {username!r}, "
            f"got {is_valid} (error: {error_msg!r})"
        )

    @pytest.mark.parametrize("username, expected_valid", TEST_VECTORS)
    def test_js_regex_matches_expected(self, username, expected_valid):
        """The JS regex (replicated in Python) matches the expected result."""
        regex_matches = JS_POSIX_USERNAME_REGEX.match(username) is not None
        assert regex_matches == expected_valid, (
            f"JS regex: expected {expected_valid} for {username!r}, "
            f"got {regex_matches}"
        )

    @pytest.mark.parametrize("username, expected_valid", TEST_VECTORS)
    def test_python_validator_and_js_regex_agree(self, username, expected_valid):
        """Both validation methods produce the same accept/reject decision."""
        is_valid, _ = validate_posix_username(username)
        regex_matches = JS_POSIX_USERNAME_REGEX.match(username) is not None
        assert is_valid == regex_matches, (
            f"Parity mismatch for {username!r}: "
            f"Python validator={is_valid}, JS regex={regex_matches}"
        )
