"""Property-based tests for POSIX username validation.

Feature: posix-username-validation

Uses Hypothesis to verify correctness properties of the shared
``validate_posix_username()`` validator defined in
``lambda/shared/validators.py``.
"""

import os
import re
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda shared module directly.
# ---------------------------------------------------------------------------
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda", "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from validators import validate_posix_username

# Reference regex used as the oracle / model specification.
_REFERENCE_REGEX = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class TestProperty1ValidatorMatchesReferenceSpec:
    """Property 1: Validator matches reference specification (model-based).

    For any arbitrary string, ``validate_posix_username()`` SHALL return
    ``(True, "")`` if and only if the string matches the reference regex
    ``^[a-z_][a-z0-9_-]{0,31}$``.

    **Validates: Requirements 1.3, 1.4, 1.5, 1.7, 2.3, 2.4**
    """

    @settings(max_examples=100)
    @given(s=st.text(min_size=0, max_size=64))
    def test_validator_agrees_with_reference_regex(self, s: str) -> None:
        """validate_posix_username(s) returns (True, '') iff s matches
        the reference POSIX username regex."""
        is_valid, error_msg = validate_posix_username(s)
        expected_valid = bool(_REFERENCE_REGEX.match(s))

        if expected_valid:
            assert is_valid is True, (
                f"Reference regex accepts {s!r} but validator rejected it "
                f"with: {error_msg!r}"
            )
            assert error_msg == "", (
                f"Validator accepted {s!r} but returned non-empty error: "
                f"{error_msg!r}"
            )
        else:
            assert is_valid is False, (
                f"Reference regex rejects {s!r} but validator accepted it"
            )
            assert error_msg != "", (
                f"Validator rejected {s!r} but returned empty error message"
            )


class TestProperty2InvalidInputsProduceDescriptiveErrors:
    """Property 2: Invalid inputs produce descriptive error messages.

    For any string that does NOT match the POSIX username specification,
    ``validate_posix_username()`` SHALL return ``(False, <non-empty message>)``
    where the error message is a non-empty, human-readable string describing
    the first rule violated.

    **Validates: Requirements 2.2**
    """

    @settings(max_examples=100)
    @given(
        s=st.text(min_size=0, max_size=64).filter(
            lambda s: not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", s)
        )
    )
    def test_invalid_inputs_return_non_empty_error_message(self, s: str) -> None:
        """validate_posix_username(s) returns (False, non-empty string)
        for every string that does not match the POSIX username regex."""
        is_valid, error_msg = validate_posix_username(s)

        assert is_valid is False, (
            f"Expected validator to reject {s!r} but it returned valid"
        )
        assert isinstance(error_msg, str), (
            f"Expected error message to be a string, got {type(error_msg)}"
        )
        assert len(error_msg) > 0, (
            f"Validator rejected {s!r} but returned an empty error message"
        )


# ---------------------------------------------------------------------------
# Path setup — load lambda cluster_operations module.
# ---------------------------------------------------------------------------
_CLUSTER_OPS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "lambda", "cluster_operations"
)
if _CLUSTER_OPS_DIR not in sys.path:
    sys.path.insert(0, _CLUSTER_OPS_DIR)

from posix_provisioning import generate_user_creation_commands


class TestProperty3ValidUsernamesProduceWellFormedCommands:
    """Property 3: Valid usernames produce well-formed shell commands.

    For any valid POSIX username and any positive integer UID/GID pair,
    ``generate_user_creation_commands(user_id, uid, gid)`` SHALL return
    a list of exactly 3 commands for ``groupadd``, ``useradd``, and
    ``chown``, where the ``user_id`` appears only as a command argument
    or in the ``/home/{user_id}`` path.

    **Validates: Requirements 5.1, 5.3**
    """

    @settings(max_examples=100)
    @given(
        user_id=st.from_regex(r"[a-z_][a-z0-9_-]{0,31}", fullmatch=True),
        uid=st.integers(min_value=1000, max_value=65534),
        gid=st.integers(min_value=1000, max_value=65534),
    )
    def test_valid_usernames_produce_exactly_three_commands(
        self, user_id: str, uid: int, gid: int
    ) -> None:
        """generate_user_creation_commands returns exactly 3 commands
        containing groupadd, useradd, and chown for valid inputs."""
        commands = generate_user_creation_commands(user_id, uid, gid)

        # Exactly 3 commands
        assert isinstance(commands, list), (
            f"Expected list, got {type(commands)}"
        )
        assert len(commands) == 3, (
            f"Expected 3 commands for valid username {user_id!r}, "
            f"got {len(commands)}: {commands}"
        )

        # Commands contain the expected utilities
        assert "groupadd" in commands[0], (
            f"First command should contain 'groupadd': {commands[0]!r}"
        )
        assert "useradd" in commands[1], (
            f"Second command should contain 'useradd': {commands[1]!r}"
        )
        assert "chown" in commands[2], (
            f"Third command should contain 'chown': {commands[2]!r}"
        )

    @settings(max_examples=100)
    @given(
        user_id=st.from_regex(r"[a-z_][a-z0-9_-]{0,31}", fullmatch=True),
        uid=st.integers(min_value=1000, max_value=65534),
        gid=st.integers(min_value=1000, max_value=65534),
    )
    def test_user_id_appears_only_in_expected_positions(
        self, user_id: str, uid: int, gid: int
    ) -> None:
        """user_id appears only as a command argument or in
        /home/{user_id} path — never in unexpected positions."""
        commands = generate_user_creation_commands(user_id, uid, gid)
        home_path = f"/home/{user_id}"

        # Allowed tokens that may contain the user_id:
        # - The user_id itself (standalone argument)
        # - The /home/{user_id} path (e.g. -d /home/jsmith)
        # - The uid:gid ownership spec (e.g. 1000:1000)
        # - Known command names and shell constructs
        allowed_commands = {"groupadd", "useradd", "chown"}
        shell_constructs = {"2>/dev/null", "||", "true"}

        for cmd in commands:
            parts = cmd.split()
            for part in parts:
                if part in allowed_commands:
                    continue
                if part in shell_constructs:
                    continue
                if part.startswith("-"):
                    # Flag or flag argument (e.g. -g, -u, -m, -d)
                    continue
                if part == user_id:
                    # Standalone username argument — expected
                    continue
                if part == home_path:
                    # Home directory path — expected
                    continue
                if part == f"{uid}:{gid}":
                    # Ownership spec — expected
                    continue
                if part.isdigit():
                    # Numeric UID or GID argument — expected
                    continue
                # Anything else is unexpected
                assert False, (
                    f"Unexpected token {part!r} in command: {cmd!r}"
                )


class TestProperty4InvalidUsernamesProduceEmptyCommandLists:
    """Property 4: Invalid usernames produce empty command lists.

    For any string that does NOT match the POSIX username specification,
    ``generate_user_creation_commands(user_id, uid, gid)`` SHALL return
    an empty list, regardless of the UID/GID values provided.

    **Validates: Requirements 5.2**
    """

    @settings(max_examples=100)
    @given(
        user_id=st.text(min_size=0, max_size=64).filter(
            lambda s: not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", s)
        ),
        uid=st.integers(min_value=1000, max_value=65534),
        gid=st.integers(min_value=1000, max_value=65534),
    )
    def test_invalid_usernames_return_empty_list(
        self, user_id: str, uid: int, gid: int
    ) -> None:
        """generate_user_creation_commands returns an empty list for
        every username that does not match the POSIX username regex."""
        commands = generate_user_creation_commands(user_id, uid, gid)

        assert isinstance(commands, list), (
            f"Expected list, got {type(commands)}"
        )
        assert len(commands) == 0, (
            f"Expected empty list for invalid username {user_id!r}, "
            f"but got {len(commands)} commands: {commands}"
        )
