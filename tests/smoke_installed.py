"""Exercise the installed MCP entry point in an environment with the MCP extra."""

import asyncio
import json
import shutil
import sysconfig
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke():
    command = shutil.which("stream-clipping-mcp", path=sysconfig.get_path("scripts"))
    if command is None:
        raise RuntimeError("stream-clipping-mcp is missing from this Python environment")

    with TemporaryDirectory() as directory:
        clips = Path(directory)
        clip_id = "example-1794660000"
        (clips / f"{clip_id}.mp4").write_bytes(b"synthetic clip")
        (clips / f"{clip_id}_insights.json").write_text(
            json.dumps({"channel": "example", "trigger": "chat"}), encoding="utf-8",
        )
        server = StdioServerParameters(command=command, args=["--clips-dir", str(clips)])
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {"list_clips"}, tools
                result = await session.call_tool("list_clips", {"limit": 1})
                assert not result.isError, result
                assert result.structuredContent == {"clips": [
                    {"clip_id": clip_id, "channel": "example", "timestamp": 1794660000.0,
                     "trigger": "chat"},
                ]}, result


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(smoke(), timeout=20))
