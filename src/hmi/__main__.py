"""Start the edge HMI.

    python -m src.hmi                              # demo package (simulated plant) on first start
    python -m src.hmi --package site.json          # first start from a configuration package
    python -m src.hmi --host 0.0.0.0               # serve the local plant network

All configuration (assets, PLC connections, I/O, users) lives in the device
database (``--db``, default ``data/edge.db``). A package is only loaded when
the database is empty; after that, change things in the web UI or load a new
package from the Commissioning tab.
"""

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Edge web HMI")
    parser.add_argument("--db", help="device database file (default data/edge.db)")
    parser.add_argument("--package", help="configuration package to load if the database is empty")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to listen on; use 0.0.0.0 to serve the plant network")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    for env, value in (("HMI_DB", args.db), ("HMI_PACKAGE", args.package)):
        if value is not None:
            os.environ[env] = value

    uvicorn.run("src.hmi.server:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
