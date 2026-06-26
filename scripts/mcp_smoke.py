"""End-to-end smoke test for the mindex MCP server over stdio."""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command="python3", args=["-m", "mindex.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

            # keyword mode avoids loading the embedding model (keeps stdout clean)
            res = await session.call_tool(
                "search_history",
                {"query": "grype scan engine", "mode": "keyword", "limit": 3},
            )
            print("\n=== search_history ===")
            print(res.content[0].text[:600])

            rec = await session.call_tool("list_recent", {"limit": 2})
            print("\n=== list_recent ===")
            print(rec.content[0].text[:400])

            # pull a session id from the recent list and fetch it
            first_line = rec.content[0].text.splitlines()[0]
            sid = first_line.split("[", 1)[1].split("]", 1)[0]
            full = await session.call_tool("get_session", {"session_id": sid})
            print("\n=== get_session", sid, "===")
            print(full.content[0].text[:400])


if __name__ == "__main__":
    asyncio.run(main())
