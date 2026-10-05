"""API entrypoint: `python apps/api/main.py` (equivalent to `ta api`)."""
import sys

from tradingagent.cli.main import main

if __name__ == "__main__":
    sys.exit(main(["api", *sys.argv[1:]]))
