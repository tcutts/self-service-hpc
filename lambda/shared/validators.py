"""Input validation utilities for Lambda handlers.

Provides reusable validation functions that enforce data format
constraints before values reach external services (Cognito, DynamoDB)
or are interpolated into shell commands.

Usage from any handler::

    from validators import validate_posix_username

    is_valid, error_msg = validate_posix_username(user_id)
    if not is_valid:
        raise ValidationError(error_msg, {"field": "userId"})
"""

import re

# Canonical POSIX username regex: starts with lowercase letter or underscore,
# followed by 0-31 lowercase letters, digits, underscores, or hyphens.
POSIX_USERNAME_REGEX = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
POSIX_USERNAME_MAX_LENGTH = 32


def validate_posix_username(username: str) -> tuple[bool, str]:
    """Validate a string as a POSIX username.

    Checks rules in priority order and returns the first violation
    found.  A valid POSIX username starts with a lowercase letter or
    underscore, contains only lowercase letters, digits, underscores,
    and hyphens, and is between 1 and 32 characters long.

    Parameters
    ----------
    username : str
        The candidate username to validate.

    Returns
    -------
    tuple[bool, str]
        A tuple of (is_valid, error_message).  When valid,
        error_message is an empty string.  When invalid,
        error_message describes the first rule violated.
    """
    if not username:
        return False, "userId is required."

    if len(username) > POSIX_USERNAME_MAX_LENGTH:
        return False, (
            f"userId must be at most {POSIX_USERNAME_MAX_LENGTH} "
            f"characters (got {len(username)})."
        )

    if username[0] not in "abcdefghijklmnopqrstuvwxyz_":
        return False, (
            "userId must start with a lowercase letter (a-z) "
            "or underscore (_)."
        )

    if not POSIX_USERNAME_REGEX.match(username):
        return False, (
            "userId contains invalid characters. Only lowercase "
            "letters (a-z), digits (0-9), underscores (_), and "
            "hyphens (-) are allowed."
        )

    return True, ""
