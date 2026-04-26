"""Authorisation helpers for the Cluster Operations API.

Extracts caller identity and group membership from the API Gateway
request context populated by the Cognito authoriser. Provides checks
for Administrator, Project Administrator, and Project User roles.
"""

from typing import Any


def get_caller_identity(event: dict[str, Any]) -> str:
    """Extract the caller's user identifier from the request context."""
    claims = _get_claims(event)
    return claims.get("cognito:username", claims.get("sub", "unknown"))


def get_caller_groups(event: dict[str, Any]) -> list[str]:
    """Extract the caller's Cognito group memberships."""
    claims = _get_claims(event)
    groups_claim = claims.get("cognito:groups", "")

    if not groups_claim:
        return []

    if isinstance(groups_claim, list):
        return groups_claim

    cleaned = groups_claim.strip("[]")
    return [g.strip() for g in cleaned.split(",") if g.strip()]


def is_administrator(event: dict[str, Any]) -> bool:
    """Return True if the caller belongs to the Administrators group."""
    return "Administrators" in get_caller_groups(event)


def is_project_admin(event: dict[str, Any], project_id: str) -> bool:
    """Return True if the caller is a Project Administrator for the given project."""
    groups = get_caller_groups(event)
    return f"ProjectAdmin-{project_id}" in groups or "Administrators" in groups


def is_project_user(event: dict[str, Any], project_id: str) -> bool:
    """Return True if the caller is authorised for the given project.

    Project Users, Project Admins, and platform Administrators all
    have project-level access.
    """
    groups = get_caller_groups(event)
    return (
        f"ProjectUser-{project_id}" in groups
        or f"ProjectAdmin-{project_id}" in groups
        or "Administrators" in groups
    )


def _get_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Safely extract claims from the API Gateway request context."""
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )
