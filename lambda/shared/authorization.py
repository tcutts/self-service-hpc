"""Unified authorisation helpers for all Lambda handlers.

Extracts caller identity and group membership from the API Gateway
request context populated by the Cognito authoriser.  Provides checks
for Platform Administrator, Project Administrator, and End User roles.

All authorisation decisions derive from the ``cognito:groups`` JWT claim.
The DynamoDB ``role`` field is NEVER read for access control.

This module replaces the per-package ``auth.py`` files and is distributed
via the shared Lambda Layer.
"""

from typing import Any


def get_caller_identity(event: dict[str, Any]) -> str:
    """Extract the caller's user identifier from the request context.

    The Cognito authoriser populates claims in the request context
    under ``requestContext.authorizer.claims``.  Prefers
    ``cognito:username``, falls back to ``sub``.
    """
    claims = _get_claims(event)
    return claims.get("cognito:username", claims.get("sub", "unknown"))


def get_caller_groups(event: dict[str, Any]) -> list[str]:
    """Extract the caller's Cognito group memberships.

    Cognito encodes groups as a comma-separated string or a JSON
    array in the ``cognito:groups`` claim.
    """
    claims = _get_claims(event)
    groups_claim = claims.get("cognito:groups", "")

    if not groups_claim:
        return []

    if isinstance(groups_claim, list):
        return groups_claim

    # Cognito may encode groups as "[group1, group2]" or "group1,group2"
    cleaned = groups_claim.strip("[]")
    return [g.strip() for g in cleaned.split(",") if g.strip()]


def is_administrator(event: dict[str, Any]) -> bool:
    """Return True if the caller belongs to the Administrators group."""
    return "Administrators" in get_caller_groups(event)


def is_authenticated(event: dict[str, Any]) -> bool:
    """Return True if the caller has a valid identity (any authenticated user)."""
    claims = _get_claims(event)
    return bool(claims.get("cognito:username") or claims.get("sub"))


def is_project_admin(event: dict[str, Any], project_id: str) -> bool:
    """Return True if the caller is a Project Administrator for *project_id*.

    Platform Administrators are implicitly project admins for every project.
    """
    groups = get_caller_groups(event)
    return f"ProjectAdmin-{project_id}" in groups or "Administrators" in groups


def is_project_user(event: dict[str, Any], project_id: str) -> bool:
    """Return True if the caller is authorised for *project_id*.

    Project Users, Project Admins, and Platform Administrators all
    have project-level access.
    """
    groups = get_caller_groups(event)
    return (
        f"ProjectUser-{project_id}" in groups
        or f"ProjectAdmin-{project_id}" in groups
        or "Administrators" in groups
    )


def get_admin_project_ids(event: dict[str, Any]) -> list[str]:
    """Extract project IDs for which the caller is a Project Administrator.

    Parses ``ProjectAdmin-{projectId}`` group names from the caller's
    Cognito groups and returns the list of project IDs.
    """
    prefix = "ProjectAdmin-"
    return [
        group[len(prefix):]
        for group in get_caller_groups(event)
        if group.startswith(prefix)
    ]


def get_member_project_ids(event: dict[str, Any]) -> list[str]:
    """Extract project IDs for which the caller has any project membership.

    Parses both ``ProjectAdmin-{projectId}`` and
    ``ProjectUser-{projectId}`` group names and returns a deduplicated
    list of project IDs.
    """
    admin_prefix = "ProjectAdmin-"
    user_prefix = "ProjectUser-"
    project_ids: set[str] = set()
    for group in get_caller_groups(event):
        if group.startswith(admin_prefix):
            project_ids.add(group[len(admin_prefix):])
        elif group.startswith(user_prefix):
            project_ids.add(group[len(user_prefix):])
    return list(project_ids)


def _get_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Safely extract claims from the API Gateway request context."""
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )
