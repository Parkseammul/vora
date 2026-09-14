"""Standalone MCP Streamable HTTP server for VORA's Notion tools."""

from collections.abc import Callable
from typing import Protocol

from mcp.server.mcpserver import MCPServer

from app.notion_client import MarketingDocument, NotionClient


class NotionToolsClient(Protocol):
    """The small Notion surface exposed through MCP."""

    def get_brand_guide(self) -> str: ...

    def search_marketing_docs(self, query: str, limit: int = 5) -> list[MarketingDocument]: ...

    def save_content_plan(self, title: str, content: str) -> tuple[str, str]: ...


def create_server(
    client_factory: Callable[[], NotionToolsClient] = NotionClient.from_environment,
) -> MCPServer:
    """Create the SDK-owned MCP server; the client is constructed only per tool call."""
    server = MCPServer(name="vora-notion", version="1.0")

    @server.tool(description="Read the configured Notion brand guide.")
    def get_brand_guide() -> dict[str, str]:
        return {"text": client_factory().get_brand_guide()}

    @server.tool(description="Search Notion marketing documents.")
    def search_marketing_docs(query: str, limit: int = 5) -> list[dict[str, str]]:
        documents = client_factory().search_marketing_docs(query, limit)
        return [
            {
                "page_id": document.page_id,
                "title": document.title,
                "url": document.url,
                "excerpt": document.excerpt,
            }
            for document in documents
        ]

    @server.tool(description="Save a VORA content plan below the configured Notion page.")
    def save_content_plan(title: str, content: str) -> dict[str, str]:
        page_id, url = client_factory().save_content_plan(title, content)
        return {"page_id": page_id, "url": url}

    return server


server = create_server()


if __name__ == "__main__":
    server.run(transport="streamable-http", host="127.0.0.1", port=8001)
