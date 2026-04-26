"""Authorisation helpers for API Gateway Cognito-authorised requests.

Extracts caller identity and group membership from the API Gateway
request context populated by the Cognito authoriser.
"""

from typing import Any


def get_caller_identity(event: dict[str, Any]) -> str:
    """Extract the caller's user identifier from the request context.

    The Cognito authoriser populates claims in the request context
    under requestContext.authorizer.claims.
    """
    claims = _get_claims(event)
    # Prefer 'cognito:username' then 'sub' as the user identifier
    return claims.get("cognito:username", claims.get("sub", "unknown"))


def get_caller_groups(event: dict[str, Any]) -> list[str]:
    """Extract the caller's Cognito group memberships.

    Cognito encodes groups as a comma-separated string or a JSON
    array in the 'cognito:groups' claim.
    """
    claims = _get_claims(event)
    groups_claim = claims.get("cognito:groups", "")

    if not groups_claim:
        return []

    # Cognito may encode groups as "[group1, group2]" or "group1,group2"
    if isinstance(groups_claim, list):
        return groups_claim

    cleaned = groups_claim.strip("[]")
    return [g.strip() for g in cleaned.split(",") if g.strip()]


def is_administrator(event: dict[str, Any]) -> bool:
    """Return True if the caller belongs to the Administrators group."""
    return "Administrators" in get_caller_groups(event)


def _get_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Safely extract claims from the API Gateway request context."""
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )
