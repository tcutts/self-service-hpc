"""Unit tests for POSIX username validation.

Feature: posix-username-validation

Example-based tests for the shared ``validate_posix_username()`` validator
defined in ``lambda/shared/validators.py``.

Validates: Requirements 2.1, 2.2, 2.3, 2.4
"""

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup — load lambda shared module directly.
# ---------------------------------------------------------------------------
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda", "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from validators import (
    POSIX_USERNAME_MAX_LENGTH,
    POSIX_USERNAME_REGEX,
    validate_posix_username,
)


# ── Constants ──────────────────────────────────────────────────────────────

class TestConstants:
    """Verify exported constants have the expected values."""

    def test_max_length_is_32(self) -> None:
        assert POSIX_USERNAME_MAX_LENGTH == 32

    def test_regex_accepts_simple_username(self) -> None:
        assert POSIX_USERNAME_REGEX.match("jsmith")

    def test_regex_rejects_empty_string(self) -> None:
        assert not POSIX_USERNAME_REGEX.match("")


# ── Valid usernames ────────────────────────────────────────────────────────

class TestValidUsernames:
    """Validate that well-formed POSIX usernames are accepted."""

    @pytest.mark.parametrize(
        "username",
        [
            "a",
            "jsmith",
            "_admin",
            "dev-user-01",
            "a" * 32,
        ],
        ids=[
            "single-lowercase-letter",
            "typical-username",
            "underscore-start",
            "hyphens-and-digits",
            "max-length-32",
        ],
    )
    def test_valid_username_accepted(self, username: str) -> None:
        is_valid, error_msg = validate_posix_username(username)
        assert is_valid is True, f"Expected {username!r} to be valid"
        assert error_msg == ""


# ── Invalid usernames ──────────────────────────────────────────────────────

class TestInvalidUsernames:
    """Validate that rule-violating usernames are rejected."""

    @pytest.mark.parametrize(
        "username",
        [
            "",
            "A",
            "1user",
            "-user",
            "user@corp",
            "user.name",
            "user name",
            "a" * 33,
        ],
        ids=[
            "empty-string",
            "uppercase-letter",
            "starts-with-digit",
            "starts-with-hyphen",
            "contains-at-sign",
            "contains-dot",
            "contains-space",
            "exceeds-max-length",
        ],
    )
    def test_invalid_username_rejected(self, username: str) -> None:
        is_valid, error_msg = validate_posix_username(username)
        assert is_valid is False, f"Expected {username!r} to be invalid"
        assert error_msg != ""


# ── Error message specificity ──────────────────────────────────────────────

class TestErrorMessages:
    """Verify each rule violation produces the correct error message."""

    def test_empty_username_message(self) -> None:
        _, msg = validate_posix_username("")
        assert msg == "userId is required."

    def test_too_long_username_message(self) -> None:
        long_name = "a" * 33
        _, msg = validate_posix_username(long_name)
        assert msg == "userId must be at most 32 characters (got 33)."

    def test_invalid_start_digit_message(self) -> None:
        _, msg = validate_posix_username("1user")
        assert msg == (
            "userId must start with a lowercase letter (a-z) "
            "or underscore (_)."
        )

    def test_invalid_start_hyphen_message(self) -> None:
        _, msg = validate_posix_username("-user")
        assert msg == (
            "userId must start with a lowercase letter (a-z) "
            "or underscore (_)."
        )

    def test_invalid_start_uppercase_message(self) -> None:
        _, msg = validate_posix_username("A")
        assert msg == (
            "userId must start with a lowercase letter (a-z) "
            "or underscore (_)."
        )

    def test_invalid_chars_at_sign_message(self) -> None:
        _, msg = validate_posix_username("user@corp")
        assert msg == (
            "userId contains invalid characters. Only lowercase "
            "letters (a-z), digits (0-9), underscores (_), and "
            "hyphens (-) are allowed."
        )

    def test_invalid_chars_dot_message(self) -> None:
        _, msg = validate_posix_username("user.name")
        assert msg == (
            "userId contains invalid characters. Only lowercase "
            "letters (a-z), digits (0-9), underscores (_), and "
            "hyphens (-) are allowed."
        )

    def test_invalid_chars_space_message(self) -> None:
        _, msg = validate_posix_username("user name")
        assert msg == (
            "userId contains invalid characters. Only lowercase "
            "letters (a-z), digits (0-9), underscores (_), and "
            "hyphens (-) are allowed."
        )


# ── Edge cases ─────────────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge cases: single-char usernames, all-digit body, all-hyphen body."""

    def test_single_lowercase_letter(self) -> None:
        is_valid, error_msg = validate_posix_username("a")
        assert is_valid is True
        assert error_msg == ""

    def test_single_underscore(self) -> None:
        is_valid, error_msg = validate_posix_username("_")
        assert is_valid is True
        assert error_msg == ""

    def test_all_digit_body(self) -> None:
        is_valid, error_msg = validate_posix_username("a123")
        assert is_valid is True
        assert error_msg == ""

    def test_all_hyphen_body(self) -> None:
        is_valid, error_msg = validate_posix_username("a---")
        assert is_valid is True
        assert error_msg == ""
