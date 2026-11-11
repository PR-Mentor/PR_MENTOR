"""Replaceable OpenAI-compatible JSON completion client."""

import httpx

from app.config import http_timeout, llm_configuration


class AIReviewUnavailable(RuntimeError):
    """AI is unconfigured, unreachable, or returned an invalid response."""


def is_configured() -> bool:
    key, model, _ = llm_configuration()
    return bool(key and model)


async def complete_json(system_prompt: str, user_prompt: str) -> str:
    api_key, model, endpoint = llm_configuration()
    if not api_key or not model:
        raise AIReviewUnavailable("AI review is unavailable because LLM_API_KEY and LLM_MODEL are not configured.")
    if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1") and not endpoint.startswith("http://localhost"):
        raise AIReviewUnavailable("AI review endpoint must use HTTPS (or localhost for development).")
    try:
        async with httpx.AsyncClient(timeout=http_timeout(), follow_redirects=False) as client:
            response = await client.post(endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            })
    except httpx.TimeoutException as exc:
        raise AIReviewUnavailable("AI provider did not respond in time.") from exc
    except httpx.RequestError as exc:
        raise AIReviewUnavailable("AI provider is temporarily unavailable.") from exc
    if response.status_code >= 400:
        raise AIReviewUnavailable(f"AI provider returned HTTP {response.status_code}.")
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise AIReviewUnavailable("AI provider returned a malformed response.") from exc
    if not isinstance(content, str):
        raise AIReviewUnavailable("AI provider response did not contain JSON text.")
    return content
