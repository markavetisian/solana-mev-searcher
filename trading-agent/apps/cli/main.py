"""CLI entrypoint: `python apps/cli/main.py <command>` (equivalent to `ta <command>`)."""
import sys

from tradingagent.cli.main import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
