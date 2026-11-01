"""Start the edge HMI.

    python -m src.hmi                              # starts unconfigured: load a package in the browser
    python -m src.hmi --package site.json          # first start from a configuration package
    python -m src.hmi --demo                       # first start with the simulated demo plant
    python -m src.hmi --host 0.0.0.0               # serve the local plant network
    python -m src.hmi --tls-cert c.pem --tls-key k.pem   # HTTPS (secure cookies switch on automatically)

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
    parser.add_argument("--demo", action="store_true", help="load the simulated demo plant if the database is empty")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to listen on; use 0.0.0.0 to serve the plant network")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--tls-cert", help="certificate file (PEM) to serve HTTPS")
    parser.add_argument("--tls-key", help="private key file (PEM) for --tls-cert")
    args = parser.parse_args()

    if args.demo:
        os.environ["HMI_DEMO"] = "1"
    for env, value in (("HMI_DB", args.db), ("HMI_PACKAGE", args.package)):
        if value is not None:
            os.environ[env] = value

    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key go together")
    if args.tls_cert:
        os.environ["HMI_SECURE_COOKIES"] = "1"  # session cookie never sent over plain HTTP
    uvicorn.run("src.hmi.server:create_app", factory=True, host=args.host, port=args.port,
                ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key)


if __name__ == "__main__":
    main()
