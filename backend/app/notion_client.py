"""Small synchronous Notion REST client used by the standalone MCP server."""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx


class NotionError(RuntimeError):
    """A user-safe error: API credentials are never included in its message."""


class NotionConfigurationError(NotionError):
    pass


@dataclass(frozen=True)
class MarketingDocument:
    page_id: str
    title: str
    url: str
    excerpt: str


class NotionClient:
    _BASE_URL = "https://api.notion.com/v1"
    _VERSION = "2022-06-28"

    def __init__(
        self,
        api_key: str,
        brand_guide_page_id: str | None = None,
        content_plan_parent_page_id: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise NotionConfigurationError("NOTION_API_KEY is required")
        self._api_key = api_key
        self._brand_guide_page_id = brand_guide_page_id
        self._content_plan_parent_page_id = content_plan_parent_page_id
        self._http_client = http_client or httpx.Client(timeout=10.0)

    @classmethod
    def from_environment(cls) -> "NotionClient":
        return cls(
            api_key=os.environ.get("NOTION_API_KEY", ""),
            brand_guide_page_id=os.environ.get("NOTION_BRAND_GUIDE_PAGE_ID"),
            content_plan_parent_page_id=os.environ.get("NOTION_CONTENT_PLAN_PARENT_PAGE_ID"),
        )

    def get_brand_guide(self) -> str:
        page_id = self._required_id(self._brand_guide_page_id, "NOTION_BRAND_GUIDE_PAGE_ID")
        return "\n".join(self._block_text(page_id)).strip()

    def search_marketing_docs(self, query: str, limit: int = 5) -> list[MarketingDocument]:
        if not query.strip():
            raise NotionError("query must not be blank")
        if not 1 <= limit <= 100:
            raise NotionError("limit must be between 1 and 100")
        payload = self._request("POST", "/search", json={"query": query, "page_size": limit})
        results = payload.get("results")
        if not isinstance(results, list):
            raise NotionError("Notion returned an invalid search response")
        return [self._marketing_document(page) for page in results if isinstance(page, dict)][:limit]

    def save_content_plan(self, title: str, content: str) -> tuple[str, str]:
        if not title.strip() or not content.strip():
            raise NotionError("title and content must not be blank")
        parent_id = self._required_id(
            self._content_plan_parent_page_id, "NOTION_CONTENT_PLAN_PARENT_PAGE_ID"
        )
        payload = self._request(
            "POST",
            "/pages",
            json={
                "parent": {"type": "page_id", "page_id": parent_id},
                "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
                "children": list(self._paragraph_blocks(content)),
            },
        )
        page_id, url = payload.get("id"), payload.get("url")
        if not isinstance(page_id, str) or not isinstance(url, str):
            raise NotionError("Notion returned an invalid page creation response")
        return page_id, url

    def _block_text(self, block_id: str) -> list[str]:
        cursor: str | None = None
        text: list[str] = []
        while True:
            params: dict[str, str | int] = {"page_size": 100}
            if cursor is not None:
                params["start_cursor"] = cursor
            payload = self._request("GET", f"/blocks/{block_id}/children", params=params)
            blocks = payload.get("results")
            if not isinstance(blocks, list):
                raise NotionError("Notion returned an invalid block response")
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                value = self._plain_text(block)
                if value:
                    text.append(value)
                if block.get("has_children") is True and isinstance(block.get("id"), str):
                    text.extend(self._block_text(block["id"]))
            if payload.get("has_more") is not True:
                return text
            cursor = payload.get("next_cursor")
            if not isinstance(cursor, str):
                raise NotionError("Notion returned an invalid pagination cursor")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http_client.request(
                method,
                f"{self._BASE_URL}{path}",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Notion-Version": self._VERSION,
                    "Content-Type": "application/json",
                },
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise NotionError("Notion request timed out") from exc
        except httpx.HTTPError as exc:
            raise NotionError("Notion request failed") from exc
        if response.status_code in (401, 403):
            raise NotionError("Notion authentication or access was denied")
        if response.status_code == 404:
            raise NotionError("Notion resource was not found")
        if response.status_code == 429:
            raise NotionError("Notion rate limit exceeded; try again later")
        if response.status_code >= 500:
            raise NotionError("Notion service is temporarily unavailable")
        if response.status_code >= 400:
            raise NotionError("Notion rejected the request")
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotionError("Notion returned an invalid response") from exc
        if not isinstance(payload, dict):
            raise NotionError("Notion returned an invalid response")
        return payload

    @staticmethod
    def _required_id(value: str | None, variable: str) -> str:
        if value is None or not value.strip():
            raise NotionConfigurationError(f"{variable} is required")
        return value

    @staticmethod
    def _plain_text(block: dict[str, Any]) -> str:
        block_type = block.get("type")
        value = block.get(block_type) if isinstance(block_type, str) else None
        rich_text = value.get("rich_text") if isinstance(value, dict) else None
        if not isinstance(rich_text, list):
            return ""
        return "".join(item.get("plain_text", "") for item in rich_text if isinstance(item, dict))

    @staticmethod
    def _marketing_document(page: dict[str, Any]) -> MarketingDocument:
        properties = page.get("properties")
        title = ""
        if isinstance(properties, dict):
            for value in properties.values():
                if isinstance(value, dict) and isinstance(value.get("title"), list):
                    title = "".join(
                        part.get("plain_text", "") for part in value["title"] if isinstance(part, dict)
                    )
                    if title:
                        break
        page_id = page.get("id")
        url = page.get("url")
        if not isinstance(page_id, str) or not isinstance(url, str):
            raise NotionError("Notion returned an invalid search result")
        return MarketingDocument(page_id, title or "Untitled", url, str(page.get("excerpt", "")))

    @staticmethod
    def _paragraph_blocks(content: str) -> Iterable[dict[str, object]]:
        for paragraph in content.splitlines() or [content]:
            for chunk in (paragraph[i : i + 2000] for i in range(0, len(paragraph), 2000)):
                if chunk:
                    yield {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}}
