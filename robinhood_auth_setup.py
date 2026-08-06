"""One-time setup: authorizes this app against your Robinhood Agentic Trading
account via OAuth (your browser will open once), then prints the tools the
server exposes so we can confirm the connection works before wiring up real
order placement.

Usage:
    python3 robinhood_auth_setup.py
"""
from robinhood_mcp_client import RobinhoodMCPClient


def main():
    client = RobinhoodMCPClient()
    print("Connecting to Robinhood's Agentic Trading MCP server...")
    print("Your browser should open for a one-time authorization -- approve access there.\n")
    client.start()
    print("Connected. Available tools:\n")
    result = client.list_tools()
    for tool in result.tools:
        print(f"- {tool.name}: {tool.description}")
    client.stop()
    print("\nDone. Re-run this anytime to confirm the saved login still works.")


if __name__ == "__main__":
    main()
