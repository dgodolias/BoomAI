from __future__ import annotations

import asyncio
from typing import Callable

import httpx


class GeminiClient:
    """HTTP transport client for Gemini generateContent calls."""

    async def post(
        self,
        *,
        url: str,
        payload: dict,
        timeout: float,
        max_retries: int,
        on_retry: Callable[[int, str], None] | None = None,
    ) -> httpx.Response | None:
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    return await client.post(
                        url,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    )
            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.ReadError,
                httpx.ProtocolError,
                httpx.RemoteProtocolError,
            ) as error:
                error_type = type(error).__name__
                if attempt < max_retries - 1:
                    wait_seconds = 2 ** (attempt + 1)
                    if on_retry:
                        on_retry(attempt + 1, f"{error_type}, retrying in {wait_seconds}s")
                    await asyncio.sleep(wait_seconds)
                else:
                    if on_retry:
                        on_retry(attempt + 1, f"{error_type}, giving up after {max_retries} attempts")
                    return None
        return None
