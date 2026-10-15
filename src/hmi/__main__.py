"""Start the web HMI.

    python -m src.hmi                                   # built-in process simulator
    python -m src.hmi --driver modbus --plc 192.168.1.10  # real PLC over Modbus TCP
"""

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="PLC web HMI")
    parser.add_argument("--driver", choices=["sim", "modbus"], default=os.environ.get("HMI_DRIVER", "sim"))
    parser.add_argument("--plc", help="PLC IP address (Modbus driver)")
    parser.add_argument("--plc-port", type=int, help="Modbus TCP port (default 502)")
    parser.add_argument("--unit", type=int, help="Modbus unit/device id (default 1)")
    parser.add_argument("--tags", help="path to a tags.json file")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to listen on; use 0.0.0.0 to serve the plant network")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    os.environ["HMI_DRIVER"] = args.driver
    for env, value in (("HMI_MODBUS_HOST", args.plc), ("HMI_MODBUS_PORT", args.plc_port),
                       ("HMI_MODBUS_UNIT", args.unit), ("HMI_TAGS", args.tags)):
        if value is not None:
            os.environ[env] = str(value)

    uvicorn.run("src.hmi.server:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
