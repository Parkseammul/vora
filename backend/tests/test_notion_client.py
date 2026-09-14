import httpx
import pytest

from app.notion_client import NotionClient, NotionConfigurationError, NotionError


def client(handler: httpx.MockTransport) -> NotionClient:
    return NotionClient("secret", "brand", "parent", httpx.Client(transport=handler))


def test_brand_guide_handles_nested_blocks_and_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/brand/children"):
            if request.url.params.get("start_cursor") == "next":
                return httpx.Response(200, json={"results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Next"}]}, "has_children": False}], "has_more": False})
            return httpx.Response(200, json={"results": [{"id": "nested", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Top"}]}, "has_children": True}], "has_more": True, "next_cursor": "next"})
        if request.url.path.endswith("/nested/children"):
            return httpx.Response(200, json={"results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Child"}]}, "has_children": False}], "has_more": False})
    assert client(httpx.MockTransport(handler)).get_brand_guide() == "Top\nChild\nNext"


def test_search_save_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": [{"id": "p1", "url": "https://notion/p1", "properties": {"Name": {"title": [{"plain_text": "Doc"}]}}}]})
        return httpx.Response(200, json={"id": "new", "url": "https://notion/new"})
    notion = client(httpx.MockTransport(handler))
    assert notion.search_marketing_docs("campaign")[0].title == "Doc"
    assert notion.save_content_plan("Plan", "content") == ("new", "https://notion/new")
    with pytest.raises(NotionConfigurationError):
        NotionClient("", "brand")


@pytest.mark.parametrize(
    ("status_code", "error_message"),
    [(401, "authentication"), (404, "not found"), (429, "rate limit")],
)
def test_notion_http_errors_are_safe_and_actionable(status_code: int, error_message: str) -> None:
    with pytest.raises(NotionError, match=error_message):
        client(httpx.MockTransport(lambda _: httpx.Response(status_code))).get_brand_guide()
