import logging
from trader import CopyTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

if __name__ == "__main__":
    CopyTrader().run()
