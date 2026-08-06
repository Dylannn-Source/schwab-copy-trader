"""Runs the Discord alert listener standalone, no Flask needed.

Defaults to DRY RUN — alerts are parsed and logged but no orders are placed.
Set "live_trading": true in config.json (plus "robinhood_account_number") to
place real orders through Robinhood's Agentic Trading MCP instead.
"""
import json
import logging
import signal
import sys
import time
from pathlib import Path

from discord_trader import DiscordCopyTrader, DryRunExecutor
from kill_switch_executor import RemoteKillSwitchExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PAUSE_FLAG_URL = "https://raw.githubusercontent.com/Dylannn-Source/schwab-copy-trader/claude/discord-alerts-robinhood-copy-ulxwpu/PAUSE_FLAG"


class ConsoleActivityLog:
    def append(self, level: str, message: str):
        logging.log(getattr(logging, level, logging.INFO), message)


def build_executor(config: dict, activity_log):
    if not config.get("live_trading"):
        activity_log.append("INFO", "live_trading is off — running in DRY RUN mode, no real orders will be placed.")
        return DryRunExecutor(activity_log), None

    account_number = config.get("robinhood_account_number")
    if not account_number:
        sys.exit("live_trading is true but robinhood_account_number is not set in config.json.")

    from robinhood_executor import RobinhoodExecutor
    from robinhood_mcp_client import RobinhoodMCPClient

    activity_log.append("WARNING", "live_trading is ON — real orders will be placed on Robinhood.")
    rh_client = RobinhoodMCPClient(token_path=config.get("robinhood_mcp_token_path", "robinhood_mcp_token.json"))
    print("Connecting to Robinhood's Agentic Trading MCP server...")
    rh_client.start()
    print("Connected.")
    executor = RobinhoodExecutor(rh_client, account_number, activity_log)
    executor = RemoteKillSwitchExecutor(executor, PAUSE_FLAG_URL, activity_log)
    return executor, rh_client


def main():
    config_path = Path("config.json")
    if not config_path.exists():
        sys.exit("config.json not found - copy config.example.json to config.json and fill it in first.")
    config = json.loads(config_path.read_text())

    activity_log = ConsoleActivityLog()
    executor, rh_client = build_executor(config, activity_log)

    trader = DiscordCopyTrader(config, activity_log, executor=executor)
    trader.start()

    def _stop(*_):
        trader.stop()
        if rh_client:
            rh_client.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print("Listening for alerts - Ctrl+C to stop.")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
