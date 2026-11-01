"""Configuration packages: everything one edge device needs, in one JSON file.

The cloud portal will publish a package per device. Until then a package can
be loaded from a file::

    python -m src.hmi.package demo  site.json     # write the demo package
    python -m src.hmi.package import site.json    # apply it to the device database
    python -m src.hmi.package export backup.json  # current config incl. on-site edits

Package layout::

    {
      "format": "plant-hmi/edge-package", "schema": 1,
      "package_version": 3, "published_at": 1760000000, "published_by": "jane",
      "client": {"id": "CL-0001", "name": "Acme Water"},
      "site":   {"id": "ST-0001", "name": "North Pump Station", "address": "", "timezone": "Australia/Brisbane"},
      "device": {"id": "GW-0001", "name": "IoT2050 #1", "serial": ""},
      "settings": {"history_days": 7},
      "assets": [{"id": "PS-01", "name": "...", "description": "", "screen": "auto",
                  "plc": {"protocol": "modbus", "host": "192.168.1.10", "port": 502, "unit": 1, "poll_ms": 500},
                  "tags": [ ...tag definitions as in tags.json... ]}],
      "users": [{"username": "...", "role": "operator", "password_hash": "scrypt$...", ...}]
    }

Only the assets assigned to this device and the users with access to its site
are included; nothing about other clients or sites ever reaches the device.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .store import Store
from .tags import ROLES, TagDatabase, TagError

FORMAT = "plant-hmi/edge-package"
SCHEMA = 1
PROTOCOLS = ("sim", "modbus", "s7")
SCREENS = ("auto", "tank-demo")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEMO_TAGS = Path(__file__).parent / "tags.json"


class PackageError(ValueError):
    """The package is malformed or would leave the device unusable."""


def _require_id(obj: dict[str, Any], what: str) -> None:
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), str) or not ID_RE.match(obj["id"]):
        raise PackageError(f"{what} needs an id (letters, digits, . _ -)")
    if not isinstance(obj.get("name"), str) or not obj["name"].strip():
        raise PackageError(f"{what} {obj['id']} needs a name")


def validate_plc(plc: dict[str, Any], where: str = "PLC") -> dict[str, Any]:
    """Check and normalise a PLC connection block."""
    if not isinstance(plc, dict):
        raise PackageError(f"{where}: connection settings missing")
    protocol = plc.get("protocol")
    if protocol not in PROTOCOLS:
        raise PackageError(f"{where}: protocol must be one of {', '.join(PROTOCOLS)}")
    poll_ms = plc.get("poll_ms", 500)
    if not isinstance(poll_ms, int) or not 100 <= poll_ms <= 60000:
        raise PackageError(f"{where}: poll interval must be 100–60000 ms")
    clean = {"protocol": protocol, "poll_ms": poll_ms}
    if protocol == "modbus":
        host, port, unit = plc.get("host"), plc.get("port", 502), plc.get("unit", 1)
        if not isinstance(host, str) or not host.strip() or len(host) > 253:
            raise PackageError(f"{where}: PLC IP address or host name required")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise PackageError(f"{where}: port must be 1–65535")
        if not isinstance(unit, int) or not 0 <= unit <= 255:
            raise PackageError(f"{where}: unit id must be 0–255")
        clean.update(host=host.strip(), port=port, unit=unit)
    elif protocol == "s7":
        host, rack, slot, port = plc.get("host"), plc.get("rack", 0), plc.get("slot", 1), plc.get("port", 102)
        if not isinstance(host, str) or not host.strip() or len(host) > 253:
            raise PackageError(f"{where}: PLC IP address or host name required")
        if not isinstance(rack, int) or not 0 <= rack <= 7 or not isinstance(slot, int) or not 0 <= slot <= 31:
            raise PackageError(f"{where}: rack must be 0–7 and slot 0–31 (S7-1200/1500: 0/1, S7-300: 0/2)")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise PackageError(f"{where}: port must be 1–65535")
        clean.update(host=host.strip(), rack=rack, slot=slot, port=port)
    return clean


def validate_asset_io(tags: list[dict[str, Any]], mode_tag: str | None = None) -> TagDatabase:
    """Check an asset's I/O list together with its Auto/Manual mode tag. Raises TagError."""
    db = TagDatabase.from_dicts({}, tags)
    if mode_tag:
        tag = db.tags.get(mode_tag)
        if tag is None or tag.type != "bool":
            raise TagError(f"Auto/Manual mode tag {mode_tag!r} must be a digital point of this asset")
    elif any(t.manual_only for t in db.tags.values()):
        raise TagError("Points marked manual-only need the asset's Auto/Manual mode tag")
    return db


def validate_package(pkg: dict[str, Any]) -> None:
    if not isinstance(pkg, dict) or pkg.get("format") != FORMAT:
        raise PackageError("Not a configuration package (format field missing or wrong)")
    if pkg.get("schema") != SCHEMA:
        raise PackageError(f"Unsupported package schema {pkg.get('schema')!r}; this device understands {SCHEMA}")
    for key in ("client", "site", "device"):
        _require_id(pkg.get(key), key.capitalize())
    assets = pkg.get("assets")
    if not isinstance(assets, list) or not assets:
        raise PackageError("A package needs at least one asset")
    seen = set()
    for asset in assets:
        _require_id(asset, "Asset")
        if asset["id"] in seen:
            raise PackageError(f"Asset id {asset['id']} is used twice")
        seen.add(asset["id"])
        if asset.get("screen", "auto") not in SCREENS:
            raise PackageError(f"Asset {asset['id']}: screen must be one of {', '.join(SCREENS)}")
        asset["plc"] = validate_plc(asset.get("plc"), f"Asset {asset['id']}")
        try:
            validate_asset_io(asset.get("tags", []), asset.get("mode_tag"))
        except TagError as exc:
            raise PackageError(f"Asset {asset['id']}: {exc}") from None
    users = pkg.get("users")
    if users is not None:
        if not isinstance(users, list):
            raise PackageError("users must be a list")
        for user in users:
            if not isinstance(user, dict) or not user.get("username") or user.get("role") not in ROLES \
                    or not str(user.get("password_hash", "")).startswith("scrypt$"):
                raise PackageError(f"User entry {user.get('username', '?') if isinstance(user, dict) else '?'} "
                                   "needs username, a valid role and a password_hash")
        if not any(u["role"] in ("engineer", "integrator") and not u.get("disabled") for u in users):
            raise PackageError("The package's user list needs at least one active engineer or integrator, "
                               "or nobody could manage this device")


def apply_package(store: Store, pkg: dict[str, Any], applied_by: str, source: str = "file") -> dict[str, Any]:
    """Validate a package and make it this device's configuration."""
    validate_package(pkg)
    current_device = store.get_meta("device")
    if current_device and current_device["id"] != pkg["device"]["id"] and source == "cloud":
        raise PackageError(f"Package is for device {pkg['device']['id']}, this is {current_device['id']}")
    info = {"version": pkg.get("package_version", 0), "published_at": pkg.get("published_at"),
            "published_by": pkg.get("published_by"), "applied_at": time.time(), "applied_by": applied_by,
            "source": source}
    meta = {"client": pkg["client"], "site": pkg["site"], "device": pkg["device"],
            "settings": pkg.get("settings", {}), "package": info}
    users = None
    if pkg.get("users") is not None:
        defaults = {"full_name": "", "must_change_password": False, "disabled": False,
                    "created_at": time.time(), "last_login": None}
        users = [{**defaults, **u, "username": u["username"].lower()} for u in pkg["users"]]
    store.replace_configuration(meta, pkg["assets"], users)
    return info


def export_package(store: Store, include_users: bool = True) -> dict[str, Any]:
    """The device's current configuration, including edits made on site."""
    info = store.get_meta("package", {})
    pkg = {
        "format": FORMAT, "schema": SCHEMA,
        "package_version": info.get("version", 0), "published_at": info.get("published_at"),
        "published_by": info.get("published_by"),
        "exported_at": time.time(),
        "client": store.get_meta("client"), "site": store.get_meta("site"), "device": store.get_meta("device"),
        "settings": store.get_meta("settings", {}),
        "assets": [{**a, "tags": store.tags(a["id"])} for a in store.assets()],
        "local_changes": len(store.changes(pending_only=True)),
    }
    users = store.users()
    if include_users and users:  # omitted when empty: importing then leaves accounts as they are
        pkg["users"] = users
    return pkg


def demo_package() -> dict[str, Any]:
    """A self-contained package running the simulated heated tank."""
    raw = json.loads(DEMO_TAGS.read_text(encoding="utf-8"))
    return {
        "format": FORMAT, "schema": SCHEMA, "package_version": 1, "published_at": time.time(),
        "published_by": "demo",
        "client": {"id": "DEMO", "name": "Demo Client"},
        "site": {"id": "DEMO-SITE", "name": "Demo Site", "address": "", "timezone": "Australia/Brisbane"},
        "device": {"id": "DEMO-GW", "name": "Demo gateway (simulator)", "serial": ""},
        "settings": {"history_days": 7},
        "assets": [{
            "id": "SKID-01", "name": raw["plant"]["name"], "description": raw["plant"]["description"],
            "screen": "tank-demo", "plc": {"protocol": "sim", "poll_ms": 250}, "tags": raw["tags"],
            "mode_tag": "SP_AutoMode",
        }],
    }


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Manage this device's configuration package")
    parser.add_argument("--db", default=os.environ.get("HMI_DB", "data/edge.db"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import").add_argument("file")
    sub.add_parser("export").add_argument("file", nargs="?")
    sub.add_parser("demo").add_argument("file")
    args = parser.parse_args()

    if args.cmd == "demo":
        Path(args.file).write_text(json.dumps(demo_package(), indent=2), encoding="utf-8")
        print(f"Wrote demo package to {args.file}")
        return
    store = Store(args.db)
    if args.cmd == "import":
        try:
            info = apply_package(store, json.loads(Path(args.file).read_text(encoding="utf-8")), "command line")
        except (PackageError, json.JSONDecodeError) as exc:
            sys.exit(f"Package rejected: {exc}")
        print(f"Applied package version {info['version']}. Restart the HMI (or it reloads on next start).")
    else:
        text = json.dumps(export_package(store), indent=2)
        if args.file:
            Path(args.file).write_text(text, encoding="utf-8")
        else:
            print(text)


if __name__ == "__main__":
    main()
