"""Entrypoint for the DCM4CHEE monitoring MCP server (stdio transport)."""

import logging
import sys

from mcp.server.fastmcp import FastMCP

from dcm4chee_mcp import tools_logs, tools_pacs

INSTRUCTIONS = """\
Tools for monitoring and diagnosing a DCM4CHEE Archive 5.33 PACS.

Typical workflows:
- Health check: get_server_status, then get_storage_status / list_queues for detail.
- "Something failed": get_failed_tasks (queue/export/retrieve task errors) and
  get_recent_errors (log scan), then diagnose_error to get the likely cause and
  concrete resolution steps from the built-in DCM4CHEE knowledge base.
- Connectivity issues with a modality/remote PACS: list_aets, then echo_aet.
- After fixing the underlying issue: retry_task to re-queue failed tasks.
"""


def create_server() -> FastMCP:
    mcp = FastMCP("dcm4chee-monitor", instructions=INSTRUCTIONS)
    tools_pacs.register_tools(mcp)
    tools_logs.register_tools(mcp)
    return mcp


def main() -> None:
    # stdout carries the MCP stdio transport; all logging must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    create_server().run()


if __name__ == "__main__":
    main()
