"""Worker entrypoint: `python apps/worker/main.py --source ws` (equivalent to `ta worker`)."""
import sys

from tradingagent.cli.main import main

if __name__ == "__main__":
    sys.exit(main(["worker", *sys.argv[1:]]))
