import asyncio

from app.mcp_server import create_server
from app.notion_client import MarketingDocument


class FakeNotion:
    def get_brand_guide(self) -> str:
        return "Brand"

    def search_marketing_docs(self, query: str, limit: int = 5) -> list[MarketingDocument]:
        return [MarketingDocument("id", query, "url", "")]

    def save_content_plan(self, title: str, content: str) -> tuple[str, str]:
        return "id", "url"


def test_registers_and_calls_three_notion_tools() -> None:
    server = create_server(lambda: FakeNotion())

    async def inspect_tools() -> tuple[set[str], object, object]:
        tools = await server.list_tools()
        brand_result = await server.call_tool("get_brand_guide", {})
        plan_result = await server.call_tool("save_content_plan", {"title": "P", "content": "C"})
        return {tool.name for tool in tools}, brand_result, plan_result

    tool_names, brand_result, plan_result = asyncio.run(inspect_tools())
    assert tool_names == {"get_brand_guide", "search_marketing_docs", "save_content_plan"}
    assert brand_result.structured_content == {"text": "Brand"}
    assert plan_result.structured_content == {"page_id": "id", "url": "url"}
