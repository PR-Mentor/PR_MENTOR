"""Repository visibility guard for the service's shared GitHub credential."""

from app.github_service.client import GitHubAPIError, async_client, request_json


async def ensure_public_repository(owner: str, repository: str) -> None:
    """Prevent a shared server token from turning URL access into private-repo access."""
    async with async_client() as client:
        result = await request_json(client, f"/repos/{owner}/{repository}", use_cache=False)
    if not isinstance(result, dict):
        raise GitHubAPIError("GitHub returned invalid repository visibility metadata.", 502)
    if result.get("private") is not False or result.get("visibility", "public") != "public":
        raise GitHubAPIError("Private repositories require user-connected GitHub authorization.", 403)
