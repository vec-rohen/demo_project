"""Start the cloud portal.

    python -m src.cloud                 # http://127.0.0.1:8100
    python -m src.cloud --host 0.0.0.0  # reachable from other machines (put HTTPS in front)
"""

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Plant cloud portal")
    parser.add_argument("--db", help="portal database file (default data/cloud.db)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    if args.db:
        os.environ["PORTAL_DB"] = args.db
    uvicorn.run("src.cloud.server:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
