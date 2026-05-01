"""Unit tests for POSIX provisioning defensive validation.

Feature: posix-username-validation

Example-based tests for the ``generate_user_creation_commands()`` function
in ``lambda/cluster_operations/posix_provisioning.py``.  Verifies that
valid usernames produce correct shell commands and invalid usernames
return an empty list.

Validates: Requirements 5.1, 5.2, 5.3
"""

import pytest

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", os.path.join(os.path.dirname(__file__), "..", "conftest.py"))
_tc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_tc)
load_lambda_module = _tc.load_lambda_module
_ensure_shared_modules = _tc._ensure_shared_modules

# ---------------------------------------------------------------------------
# Module loading — use path-based imports to avoid sys.modules collisions.
# ---------------------------------------------------------------------------
_ensure_shared_modules()
load_lambda_module("cluster_operations", "errors")
posix_provisioning = load_lambda_module("cluster_operations", "posix_provisioning")
generate_user_creation_commands = posix_provisioning.generate_user_creation_commands


# ── Valid usernames produce correct commands ───────────────────────────────


class TestValidUsernameCommands:
    """Verify valid usernames produce the expected groupadd/useradd/chown."""

    def test_typical_username(self) -> None:
        cmds = generate_user_creation_commands("jsmith", 1001, 1001)
        assert len(cmds) == 3
        assert cmds[0] == "groupadd -g 1001 jsmith 2>/dev/null || true"
        assert cmds[1] == (
            "useradd -u 1001 -g 1001 -m -d /home/jsmith jsmith "
            "2>/dev/null || true"
        )
        assert cmds[2] == "chown 1001:1001 /home/jsmith"

    def test_underscore_start_username(self) -> None:
        cmds = generate_user_creation_commands("_admin", 2000, 2000)
        assert len(cmds) == 3
        assert cmds[0] == "groupadd -g 2000 _admin 2>/dev/null || true"
        assert cmds[1] == (
            "useradd -u 2000 -g 2000 -m -d /home/_admin _admin "
            "2>/dev/null || true"
        )
        assert cmds[2] == "chown 2000:2000 /home/_admin"

    def test_username_with_hyphens_and_digits(self) -> None:
        cmds = generate_user_creation_commands("dev-user-01", 5000, 5000)
        assert len(cmds) == 3
        assert cmds[0] == "groupadd -g 5000 dev-user-01 2>/dev/null || true"
        assert cmds[1] == (
            "useradd -u 5000 -g 5000 -m -d /home/dev-user-01 "
            "dev-user-01 2>/dev/null || true"
        )
        assert cmds[2] == "chown 5000:5000 /home/dev-user-01"

    def test_single_char_username(self) -> None:
        cmds = generate_user_creation_commands("a", 1000, 1000)
        assert len(cmds) == 3
        assert cmds[0] == "groupadd -g 1000 a 2>/dev/null || true"
        assert cmds[1] == (
            "useradd -u 1000 -g 1000 -m -d /home/a a "
            "2>/dev/null || true"
        )
        assert cmds[2] == "chown 1000:1000 /home/a"

    def test_different_uid_gid(self) -> None:
        cmds = generate_user_creation_commands("testuser", 3000, 4000)
        assert len(cmds) == 3
        assert cmds[0] == "groupadd -g 4000 testuser 2>/dev/null || true"
        assert cmds[1] == (
            "useradd -u 3000 -g 4000 -m -d /home/testuser testuser "
            "2>/dev/null || true"
        )
        assert cmds[2] == "chown 3000:4000 /home/testuser"


# ── Invalid usernames return empty list ────────────────────────────────────


class TestInvalidUsernameReturnsEmpty:
    """Verify invalid usernames produce an empty command list."""

    @pytest.mark.parametrize(
        "user_id",
        [
            "",
            "user@corp",
            "A",
            "-bad",
        ],
        ids=[
            "empty-string",
            "contains-at-sign",
            "uppercase-letter",
            "starts-with-hyphen",
        ],
    )
    def test_invalid_username_returns_empty(self, user_id: str) -> None:
        cmds = generate_user_creation_commands(user_id, 1000, 1000)
        assert cmds == []
