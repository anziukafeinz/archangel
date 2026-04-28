"""Lightweight Telegram notifier.

Unlike :mod:`archangel.telegram.bot`, this module does **not** start a polling
``Application``. It just posts messages via the Bot API using ``httpx``, so it
can run alongside (or completely independently of) the control bot.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Async one-way notifier that posts to a single chat."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id are required")
        self._token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> TelegramNotifier:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, text: str, *, parse_mode: str = "Markdown") -> bool:
        """Post a message. Returns True on success, False on failure (logged)."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            response = await self._client.post(url, json=payload)
        except httpx.HTTPError:
            logger.exception("Telegram notify HTTP error")
            return False
        if response.status_code >= 400:
            logger.warning(
                "Telegram notify failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return False
        return True
