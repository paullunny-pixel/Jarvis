"""Trello REST client. The team's board stays exactly as it is — Jarvis sits
on top (read cards, write back moves/comments/dates/assignments)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API = "https://api.trello.com/1"


class TrelloError(RuntimeError):
    pass


class TrelloClient:
    def __init__(
        self,
        key: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 45.0,
    ) -> None:
        self._auth = {"key": key, "token": token}
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **params: Any) -> Any:
        response = await self._client.request(method, f"{API}{path}", params={**self._auth, **params})
        if response.status_code != 200:
            raise TrelloError(f"{method} {path}: {response.status_code} {response.text[:200]}")
        return response.json()

    # --- Reading ---

    async def my_boards(self) -> list[dict]:
        return await self._call("GET", "/members/me/boards", fields="name,url")

    async def board_lists(self, board_id: str) -> list[dict]:
        return await self._call("GET", f"/boards/{board_id}/lists", fields="name")

    async def board_cards(self, board_id: str) -> list[dict]:
        return await self._call(
            "GET",
            f"/boards/{board_id}/cards",
            fields="name,desc,due,idList,labels,dateLastActivity,idMembers,shortUrl",
        )

    async def board_members(self, board_id: str) -> list[dict]:
        return await self._call("GET", f"/boards/{board_id}/members", fields="fullName,username")

    # --- Writing (Paul's voice feedback → the board) ---

    async def move_card(self, card_id: str, list_id: str) -> None:
        await self._call("PUT", f"/cards/{card_id}", idList=list_id)

    async def set_due(self, card_id: str, due_iso: str) -> None:
        await self._call("PUT", f"/cards/{card_id}", due=due_iso)

    async def comment(self, card_id: str, text: str) -> None:
        await self._call("POST", f"/cards/{card_id}/actions/comments", text=text)

    async def create_card(self, list_id: str, name: str, desc: str = "", due_iso: str = "") -> dict:
        params: dict[str, Any] = {"idList": list_id, "name": name}
        if desc:
            params["desc"] = desc
        if due_iso:
            params["due"] = due_iso
        return await self._call("POST", "/cards", **params)

    async def assign_member(self, card_id: str, member_id: str) -> None:
        await self._call("POST", f"/cards/{card_id}/idMembers", value=member_id)
