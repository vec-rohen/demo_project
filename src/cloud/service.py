"""Cloud portal business logic: clients, sites, devices, assets, I/O, users and
publishing configuration packages to edge devices.

Everything that ends up on a device is validated with the same code the edge
uses (``src.hmi.tags`` / ``src.hmi.package``), so a package that publishes
here always loads on the IoT2050.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from typing import Any

from src.hmi.auth import MIN_PASSWORD, USERNAME_RE, hash_password
from src.hmi.package import (FORMAT, SCHEMA, SCREENS, PackageError, validate_asset_io, validate_package,
                             validate_plc)
from src.hmi.tags import ROLES as HMI_ROLES
from src.hmi.tags import TagError

from .db import CloudDB

PORTAL_ROLES = ("admin", "technician")   # your staff; admins also manage staff accounts
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

CSV_COLUMNS = [
    "name", "description", "kind", "type", "unit", "min", "max", "decimals", "writable", "role",
    "modbus_table", "modbus_address", "modbus_scale", "modbus_type", "modbus_word_order", "modbus_bit",
    "s7_address", "s7_type", "s7_scale",
    "alarm_state", "alarm_priority", "alarm_message",
    "alarm_hihi", "alarm_hi", "alarm_lo", "alarm_lolo", "alarm_deadband", "manual_only",
]


class CloudError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now() -> float:
    return time.time()


def _need(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudError(422, f"{what} is required")
    return value.strip()


class CloudService:
    def __init__(self, db: CloudDB):
        self.db = db

    # -- audit ------------------------------------------------------------------
    def audit(self, user: str, action: str, target: str = "", detail: str = "") -> None:
        self.db.run("INSERT INTO audit (t, user, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                    (_now(), user, action, target, detail))

    def audit_log(self, limit: int = 300, target_prefix: str | None = None) -> list[dict[str, Any]]:
        if target_prefix:
            return self.db.q("SELECT * FROM audit WHERE target LIKE ? ORDER BY id DESC LIMIT ?",
                             (f"{target_prefix}%", limit))
        return self.db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # -- lookups ---------------------------------------------------------------
    def _get(self, table: str, id_: str, what: str) -> dict[str, Any]:
        row = self.db.one(f"SELECT * FROM {table} WHERE id = ?", (id_,))
        if row is None:
            raise CloudError(404, f"Unknown {what} {id_!r}")
        return row

    def client(self, client_id: str) -> dict[str, Any]:
        return self._get("clients", client_id, "client")

    def site(self, site_id: str) -> dict[str, Any]:
        return self._get("sites", site_id, "site")

    def device(self, device_id: str) -> dict[str, Any]:
        return self._get("devices", device_id, "device")

    def asset(self, asset_id: str) -> dict[str, Any]:
        row = self._get("assets", asset_id, "asset")
        row["plc"] = json.loads(row["plc"])
        return row

    # -- clients -----------------------------------------------------------------
    def list_clients(self) -> list[dict[str, Any]]:
        return self.db.q("""
            SELECT c.*,
              (SELECT COUNT(*) FROM sites s WHERE s.client_id = c.id) AS sites,
              (SELECT COUNT(*) FROM devices d WHERE d.client_id = c.id) AS devices,
              (SELECT COUNT(*) FROM assets a JOIN sites s ON a.site_id = s.id WHERE s.client_id = c.id) AS assets
            FROM clients c ORDER BY c.name""")

    def client_detail(self, client_id: str) -> dict[str, Any]:
        client = self.client(client_id)
        sites = self.db.q("""SELECT s.*, (SELECT COUNT(*) FROM assets a WHERE a.site_id = s.id) AS assets,
                                    (SELECT COUNT(*) FROM devices d WHERE d.site_id = s.id) AS devices
                             FROM sites s WHERE s.client_id = ? ORDER BY s.name""", (client_id,))
        devices = [self._device_summary(d) for d in
                   self.db.q("SELECT * FROM devices WHERE client_id = ? ORDER BY name", (client_id,))]
        users = self.list_users(client_id=client_id)
        return {**client, "sites": sites, "devices": devices, "users": users}

    def _client_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for key in ("name", "contact", "phone", "email", "notes"):
            if key in data and data[key] is not None:
                out[key] = str(data[key]).strip()
        if "name" in out and not out["name"]:
            raise CloudError(422, "Client name is required")
        if out.get("email") and not EMAIL_RE.match(out["email"]):
            raise CloudError(422, "That email address doesn't look right")
        return out

    def create_client(self, data: dict[str, Any], user: str) -> dict[str, Any]:
        fields = self._client_fields(data)
        _need(fields.get("name"), "Client name")
        client_id = self.db.next_id("CL")
        self.db.run("INSERT INTO clients (id, name, contact, phone, email, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (client_id, fields["name"], fields.get("contact", ""), fields.get("phone", ""),
                     fields.get("email", ""), fields.get("notes", ""), _now()))
        self.audit(user, "client.create", client_id, fields["name"])
        return self.client(client_id)

    def update_client(self, client_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        self.client(client_id)
        fields = self._client_fields(data)
        for key, value in fields.items():
            self.db.run(f"UPDATE clients SET {key} = ? WHERE id = ?", (value, client_id))
        self.audit(user, "client.update", client_id, ", ".join(fields))
        return self.client(client_id)

    def delete_client(self, client_id: str, user: str) -> None:
        detail = self.client_detail(client_id)
        if detail["sites"] or detail["devices"] or detail["users"]:
            raise CloudError(409, "Remove this client's sites, devices and users first")
        self.db.run("DELETE FROM clients WHERE id = ?", (client_id,))
        self.audit(user, "client.delete", client_id, detail["name"])

    # -- sites -------------------------------------------------------------------
    def _site_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("name", "address", "timezone", "notes"):
            if key in data and data[key] is not None:
                out[key] = str(data[key]).strip()
        if "name" in out and not out["name"]:
            raise CloudError(422, "Site name is required")
        if "history_days" in data and data["history_days"] is not None:
            days = data["history_days"]
            if not isinstance(days, int) or not 1 <= days <= 90:
                raise CloudError(422, "Local trend history must be 1–90 days")
            out["history_days"] = days
        return out

    def create_site(self, client_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        self.client(client_id)
        fields = self._site_fields(data)
        _need(fields.get("name"), "Site name")
        site_id = self.db.next_id("ST")
        self.db.run("""INSERT INTO sites (id, client_id, name, address, timezone, history_days, notes, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (site_id, client_id, fields["name"], fields.get("address", ""),
                     fields.get("timezone") or "Australia/Brisbane", fields.get("history_days", 7),
                     fields.get("notes", ""), _now()))
        self.audit(user, "site.create", site_id, f"{fields['name']} for {client_id}")
        return self.site(site_id)

    def site_detail(self, site_id: str) -> dict[str, Any]:
        site = self.site(site_id)
        assets = self.db.q("""SELECT a.id, a.name, a.description, a.device_id, a.screen, a.plc,
                                     (SELECT COUNT(*) FROM tags t WHERE t.asset_id = a.id) AS tags
                              FROM assets a WHERE a.site_id = ? ORDER BY a.name""", (site_id,))
        for a in assets:
            a["plc"] = json.loads(a["plc"])
        devices = [self._device_summary(d) for d in
                   self.db.q("SELECT * FROM devices WHERE site_id = ? ORDER BY name", (site_id,))]
        access = self.db.q("""SELECT sa.username, sa.role, u.full_name, u.disabled FROM site_access sa
                              JOIN users u ON u.username = sa.username WHERE sa.site_id = ? ORDER BY sa.username""",
                           (site_id,))
        return {**site, "client": self.client(site["client_id"]), "assets": assets, "devices": devices,
                "access": access}

    def update_site(self, site_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        self.site(site_id)
        fields = self._site_fields(data)
        for key, value in fields.items():
            self.db.run(f"UPDATE sites SET {key} = ? WHERE id = ?", (value, site_id))
        self.audit(user, "site.update", site_id, ", ".join(fields))
        return self.site(site_id)

    def delete_site(self, site_id: str, user: str) -> None:
        detail = self.site_detail(site_id)
        if detail["assets"] or detail["devices"]:
            raise CloudError(409, "Remove or move this site's assets and devices first")
        with self.db.transaction():
            self.db._db.execute("DELETE FROM site_access WHERE site_id = ?", (site_id,))
            self.db._db.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        self.audit(user, "site.delete", site_id, detail["name"])

    # -- devices (IoT2050 gateways) -------------------------------------------
    def _device_summary(self, device: dict[str, Any]) -> dict[str, Any]:
        last = self.db.one("SELECT version, created_at, created_by FROM packages WHERE device_id = ? "
                           "ORDER BY version DESC LIMIT 1", (device["id"],))
        assets = self.db.q("SELECT id, name FROM assets WHERE device_id = ? ORDER BY name", (device["id"],))
        return {**device, "assets": assets, "published": last,
                "unpublished_changes": self.has_unpublished_changes(device["id"], last)}

    def device_detail(self, device_id: str) -> dict[str, Any]:
        device = self._device_summary(self.device(device_id))
        site = self.site(device["site_id"]) if device["site_id"] else None
        unassigned = self.db.q("SELECT id, name FROM assets WHERE site_id = ? AND device_id IS NULL ORDER BY name",
                               (device["site_id"],)) if site else []
        versions = self.db.q("SELECT version, created_at, created_by, notes FROM packages WHERE device_id = ? "
                             "ORDER BY version DESC", (device_id,))
        _, problems = self.build_package(device_id)
        return {**device, "client": self.client(device["client_id"]), "site": site,
                "unassigned_assets": unassigned, "versions": versions, "problems": problems}

    def create_device(self, client_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        self.client(client_id)
        name = _need(data.get("name"), "Device name")
        site_id = data.get("site_id") or None
        if site_id and self.site(site_id)["client_id"] != client_id:
            raise CloudError(422, "That site belongs to another client")
        device_id = self.db.next_id("GW")
        self.db.run("INSERT INTO devices (id, client_id, site_id, name, serial, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (device_id, client_id, site_id, name, str(data.get("serial") or "").strip(),
                     str(data.get("notes") or "").strip(), _now()))
        self.audit(user, "device.create", device_id, f"{name} for {client_id}")
        return self.device(device_id)

    def update_device(self, device_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        device = self.device(device_id)
        with self.db.transaction():
            for key in ("name", "serial", "notes"):
                if data.get(key) is not None:
                    value = str(data[key]).strip()
                    if key == "name" and not value:
                        raise CloudError(422, "Device name is required")
                    self.db._db.execute(f"UPDATE devices SET {key} = ? WHERE id = ?", (value, device_id))
            if "site_id" in data and (data["site_id"] or None) != device["site_id"]:
                site_id = data["site_id"] or None
                if site_id and self.site(site_id)["client_id"] != device["client_id"]:
                    raise CloudError(422, "That site belongs to another client")
                # Assets stay at their site: moving the device releases them.
                self.db._db.execute("UPDATE assets SET device_id = NULL WHERE device_id = ?", (device_id,))
                self.db._db.execute("UPDATE devices SET site_id = ? WHERE id = ?", (site_id, device_id))
        self.audit(user, "device.update", device_id, ", ".join(k for k in data))
        return self.device(device_id)

    def delete_device(self, device_id: str, user: str) -> None:
        device = self.device(device_id)
        with self.db.transaction():
            self.db._db.execute("UPDATE assets SET device_id = NULL WHERE device_id = ?", (device_id,))
            self.db._db.execute("DELETE FROM packages WHERE device_id = ?", (device_id,))
            self.db._db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        self.audit(user, "device.delete", device_id, device["name"])

    # -- assets ------------------------------------------------------------------
    def _check_device_for_asset(self, device_id: str | None, site_id: str) -> None:
        if device_id and self.device(device_id)["site_id"] != site_id:
            raise CloudError(422, "Assign the device to this asset's site first")

    def create_asset(self, site_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        self.site(site_id)
        name = _need(data.get("name"), "Asset name")
        plc = self._plc(data.get("plc") or {"protocol": "modbus", "host": "192.168.0.1", "port": 502, "unit": 1,
                                             "poll_ms": 500})
        screen = data.get("screen") or "auto"
        if screen not in SCREENS:
            raise CloudError(422, f"Screen must be one of {', '.join(SCREENS)}")
        device_id = data.get("device_id") or None
        self._check_device_for_asset(device_id, site_id)
        asset_id = self.db.next_id("AS")
        self.db.run("""INSERT INTO assets (id, site_id, device_id, name, description, screen, plc, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (asset_id, site_id, device_id, name, str(data.get("description") or "").strip(), screen,
                     json.dumps(plc), _now()))
        self.audit(user, "asset.create", asset_id, f"{name} at {site_id}")
        return self.asset(asset_id)

    @staticmethod
    def _plc(plc: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_plc(plc)
        except PackageError as exc:
            raise CloudError(422, str(exc)) from None

    def asset_detail(self, asset_id: str) -> dict[str, Any]:
        asset = self.asset(asset_id)
        site = self.site(asset["site_id"])
        devices = self.db.q("SELECT id, name FROM devices WHERE site_id = ? ORDER BY name", (asset["site_id"],))
        return {**asset, "site": site, "client": self.client(site["client_id"]), "site_devices": devices,
                "tags": self.tags(asset_id)}

    def update_asset(self, asset_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        asset = self.asset(asset_id)
        changes = []
        if data.get("name") is not None:
            self.db.run("UPDATE assets SET name = ? WHERE id = ?", (_need(data["name"], "Asset name"), asset_id))
            changes.append("name")
        if data.get("description") is not None:
            self.db.run("UPDATE assets SET description = ? WHERE id = ?", (str(data["description"]).strip(), asset_id))
            changes.append("description")
        if data.get("screen") is not None:
            if data["screen"] not in SCREENS:
                raise CloudError(422, f"Screen must be one of {', '.join(SCREENS)}")
            self.db.run("UPDATE assets SET screen = ? WHERE id = ?", (data["screen"], asset_id))
            changes.append("screen")
        if data.get("plc") is not None:
            self.db.run("UPDATE assets SET plc = ? WHERE id = ?", (json.dumps(self._plc(data["plc"])), asset_id))
            changes.append("PLC connection")
        if "mode_tag" in data:
            mode_tag = data["mode_tag"] or None
            self._validate_tags(self.tags(asset_id), mode_tag)
            self.db.run("UPDATE assets SET mode_tag = ? WHERE id = ?", (mode_tag, asset_id))
            changes.append(f"mode tag → {mode_tag or 'none'}")
        if "device_id" in data:
            device_id = data["device_id"] or None
            self._check_device_for_asset(device_id, asset["site_id"])
            self.db.run("UPDATE assets SET device_id = ? WHERE id = ?", (device_id, asset_id))
            changes.append(f"device → {device_id or 'none'}")
        self.audit(user, "asset.update", asset_id, ", ".join(changes))
        return self.asset(asset_id)

    def delete_asset(self, asset_id: str, user: str) -> None:
        asset = self.asset(asset_id)
        with self.db.transaction():
            self.db._db.execute("DELETE FROM tags WHERE asset_id = ?", (asset_id,))
            self.db._db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        self.audit(user, "asset.delete", asset_id, asset["name"])

    def duplicate_asset(self, asset_id: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        """Copy an asset (PLC settings and all I/O) – e.g. the same pump skid at another site."""
        source = self.asset(asset_id)
        site_id = data.get("site_id") or source["site_id"]
        new = self.create_asset(site_id, {"name": data.get("name") or f"{source['name']} (copy)",
                                          "description": source["description"], "screen": source["screen"],
                                          "plc": source["plc"]}, user)
        self._replace_tags(new["id"], self.tags(asset_id))
        self.db.run("UPDATE assets SET mode_tag = ? WHERE id = ?", (source.get("mode_tag"), new["id"]))
        self.audit(user, "asset.duplicate", new["id"], f"copied from {asset_id}")
        return self.asset(new["id"])

    # -- I/O (tags) -------------------------------------------------------------
    def tags(self, asset_id: str) -> list[dict[str, Any]]:
        return [json.loads(r["def"]) for r in
                self.db.q("SELECT def FROM tags WHERE asset_id = ? ORDER BY position, name", (asset_id,))]

    @staticmethod
    def _validate_tags(tags: list[dict[str, Any]], mode_tag: str | None = None) -> None:
        try:
            validate_asset_io(tags, mode_tag)
        except TagError as exc:
            raise CloudError(422, str(exc)) from None

    def _replace_tags(self, asset_id: str, tags: list[dict[str, Any]]) -> None:
        with self.db.transaction():
            self.db._db.execute("DELETE FROM tags WHERE asset_id = ?", (asset_id,))
            for pos, tag in enumerate(tags):
                self.db._db.execute("INSERT INTO tags (asset_id, name, def, position) VALUES (?, ?, ?, ?)",
                                    (asset_id, tag["name"], json.dumps(tag), pos))

    def put_tag(self, asset_id: str, name: str, definition: dict[str, Any], user: str) -> dict[str, Any]:
        self.asset(asset_id)
        if definition.get("name") != name:
            raise CloudError(422, "The tag name in the body must match the URL")
        tags = self.tags(asset_id)
        existing = next((i for i, t in enumerate(tags) if t["name"] == name), None)
        if existing is None:
            tags.append(definition)
        else:
            tags[existing] = definition
        self._validate_tags(tags, self.asset(asset_id).get("mode_tag"))
        self._replace_tags(asset_id, tags)
        self.audit(user, "tag.update" if existing is not None else "tag.create", f"{asset_id}/{name}")
        return definition

    def delete_tag(self, asset_id: str, name: str, user: str) -> None:
        tags = self.tags(asset_id)
        remaining = [t for t in tags if t["name"] != name]
        if len(remaining) == len(tags):
            raise CloudError(404, f"Unknown tag {name!r}")
        try:
            validate_asset_io(remaining, self.asset(asset_id).get("mode_tag"))
        except TagError as exc:
            raise CloudError(422, f"Can't delete {name}: {exc}") from None
        self._replace_tags(asset_id, remaining)
        self.audit(user, "tag.delete", f"{asset_id}/{name}")

    # CSV --------------------------------------------------------------------
    @staticmethod
    def tag_to_row(tag: dict[str, Any]) -> dict[str, Any]:
        m, a = tag.get("modbus") or {}, tag.get("alarm") or {}
        yes = lambda v: "yes" if v else ""  # noqa: E731
        return {
            "name": tag["name"], "description": tag.get("description", ""), "kind": tag["kind"], "type": tag["type"],
            "unit": tag.get("unit", ""), "min": tag.get("min", ""), "max": tag.get("max", ""),
            "decimals": tag.get("decimals", "") if tag["type"] == "float" else "",
            "writable": yes(tag.get("writable")), "role": tag.get("role", "operator"),
            "modbus_table": m.get("table", ""), "modbus_address": m.get("address", ""),
            "modbus_scale": m.get("scale", "") if m else "",
            "modbus_type": m.get("type", ""), "modbus_word_order": m.get("word_order", ""), "modbus_bit": m.get("bit", ""),
            "s7_address": (tag.get("s7") or {}).get("address", ""), "s7_type": (tag.get("s7") or {}).get("type", ""),
            "s7_scale": (tag.get("s7") or {}).get("scale", "") if tag.get("s7") else "",
            "alarm_state": ("on" if a["state"] else "off") if "state" in a else "",
            "alarm_priority": a.get("priority", ""), "alarm_message": a.get("message", ""),
            "alarm_hihi": a.get("hihi", ""), "alarm_hi": a.get("hi", ""), "alarm_lo": a.get("lo", ""),
            "alarm_lolo": a.get("lolo", ""), "alarm_deadband": a.get("deadband", ""),
            "manual_only": yes(tag.get("manual_only")),
        }

    def export_csv(self, asset_id: str) -> str:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        for tag in self.tags(asset_id):
            writer.writerow(self.tag_to_row(tag))
        return out.getvalue()

    @staticmethod
    def csv_template() -> str:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerow({"name": "PumpRunning", "description": "Pump running feedback", "kind": "DI", "type": "bool",
                         "modbus_table": "discrete", "modbus_address": 0})
        writer.writerow({"name": "PumpFault", "description": "Pump VSD fault", "kind": "DI", "type": "bool",
                         "modbus_table": "discrete", "modbus_address": 1, "alarm_state": "on",
                         "alarm_priority": "high", "alarm_message": "Pump VSD fault"})
        writer.writerow({"name": "PumpStart", "description": "Pump run command", "kind": "DO", "type": "bool",
                         "writable": "yes", "modbus_table": "coil", "modbus_address": 0})
        writer.writerow({"name": "Level", "description": "Wet well level", "kind": "AI", "type": "float", "unit": "%",
                         "min": 0, "max": 100, "decimals": 1, "modbus_table": "input", "modbus_address": 0,
                         "modbus_scale": 10, "alarm_hi": 90, "alarm_lo": 10, "alarm_deadband": 1})
        writer.writerow({"name": "LevelSP", "description": "Level setpoint", "kind": "SP", "type": "float", "unit": "%",
                         "min": 10, "max": 90, "decimals": 1, "writable": "yes", "role": "engineer",
                         "modbus_table": "holding", "modbus_address": 0, "modbus_scale": 10})
        writer.writerow({"name": "FlowTotal", "description": "Flow totaliser", "kind": "AI", "type": "float", "unit": "m³",
                         "min": 0, "max": 1000000, "decimals": 0, "modbus_table": "input", "modbus_address": 2,
                         "modbus_type": "float32", "modbus_word_order": "big"})
        writer.writerow({"name": "VSDReady", "description": "VSD ready (status word bit 0)", "kind": "DI", "type": "bool",
                         "modbus_table": "input", "modbus_address": 10, "modbus_bit": 0})
        return out.getvalue()

    @staticmethod
    def row_to_tag(row: dict[str, str], line: int) -> dict[str, Any]:
        def text(key: str) -> str:
            return (row.get(key) or "").strip()

        def number(key: str, integer: bool = False) -> float | int | None:
            raw = text(key)
            if raw == "":
                return None
            try:
                value = float(raw)
            except ValueError:
                raise CloudError(422, f"Line {line}: {key} must be a number, got {raw!r}") from None
            if integer:
                if not value.is_integer():
                    raise CloudError(422, f"Line {line}: {key} must be a whole number")
                return int(value)
            return int(value) if value.is_integer() else value

        def flag(key: str) -> bool:
            return text(key).lower() in ("yes", "y", "true", "1", "x")

        kind = text("kind").upper()
        type_ = text("type").lower() or ("bool" if kind in ("DI", "DO") else "float")
        type_ = {"digital": "bool", "boolean": "bool", "analog": "float", "real": "float", "number": "float"}.get(type_, type_)
        tag: dict[str, Any] = {"name": text("name"), "kind": kind, "type": type_}
        if not tag["name"]:
            raise CloudError(422, f"Line {line}: name is required")
        if text("description"):
            tag["description"] = text("description")
        if type_ == "float":
            if text("unit"):
                tag["unit"] = text("unit")
            for key in ("min", "max"):
                if (v := number(key)) is not None:
                    tag[key] = v
            tag["decimals"] = number("decimals", integer=True) if text("decimals") else 1
        if flag("writable"):
            tag["writable"] = True
        if flag("manual_only"):
            tag["manual_only"] = True
        if text("role"):
            tag["role"] = text("role").lower()
        if text("modbus_table"):
            tag["modbus"] = {"table": text("modbus_table").lower(), "address": number("modbus_address", integer=True)}
            if type_ == "float":
                scale = number("modbus_scale")
                tag["modbus"]["scale"] = 1 if scale is None else scale
                if text("modbus_type"):
                    tag["modbus"]["type"] = text("modbus_type").lower().replace("real", "float32").replace("dint", "int32")
                if text("modbus_word_order"):
                    order = text("modbus_word_order").lower()
                    tag["modbus"]["word_order"] = {"abcd": "big", "cdab": "little"}.get(order, order)
            elif text("modbus_bit"):
                tag["modbus"]["bit"] = number("modbus_bit", integer=True)
        if text("s7_address"):
            tag["s7"] = {"address": text("s7_address")}
            if text("s7_type"):
                tag["s7"]["type"] = text("s7_type").lower()
            if type_ == "float":
                scale = number("s7_scale")
                tag["s7"]["scale"] = 1 if scale is None else scale
        if type_ == "bool" and text("alarm_state"):
            state = text("alarm_state").lower()
            if state not in ("on", "off", "true", "false", "1", "0"):
                raise CloudError(422, f"Line {line}: alarm_state must be on or off")
            tag["alarm"] = {"state": state in ("on", "true", "1"),
                            "priority": text("alarm_priority").lower() or "medium",
                            "message": text("alarm_message") or f"{tag.get('description', tag['name'])} alarm"}
        elif type_ == "float":
            alarm: dict[str, Any] = {}
            for key in ("hihi", "hi", "lo", "lolo"):
                raw = text(f"alarm_{key}")
                if raw:
                    try:
                        value = float(raw)
                        alarm[key] = int(value) if value.is_integer() else value
                    except ValueError:
                        alarm[key] = raw  # name of a setpoint tag holding the limit
            if alarm:
                if (db := number("alarm_deadband")) is not None:
                    alarm["deadband"] = db
                tag["alarm"] = alarm
        return tag

    def import_csv(self, asset_id: str, text: str, mode: str, user: str) -> dict[str, int]:
        """Import I/O from CSV. ``replace`` swaps the whole list; ``merge`` adds/updates by name."""
        self.asset(asset_id)
        if mode not in ("replace", "merge"):
            raise CloudError(422, "mode must be replace or merge")
        text = text.lstrip("﻿")
        try:
            sample = text[:2048]
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample.strip() else csv.excel
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
            raise CloudError(422, "The CSV needs a header row with at least name and kind columns "
                                  "(download the template to see the format)")
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
        imported, seen = [], set()
        for line, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            tag = self.row_to_tag(row, line)
            if tag["name"] in seen:
                raise CloudError(422, f"Line {line}: {tag['name']} appears twice")
            seen.add(tag["name"])
            imported.append(tag)
        if not imported:
            raise CloudError(422, "The CSV has no I/O rows")
        return self._apply_import(asset_id, imported, mode, user, "csv")

    def import_tia(self, asset_id: str, data: bytes, mode: str, user: str) -> dict[str, Any]:
        """Import a TIA Portal PLC tag table (.xlsx) as S7 points."""
        from .tia import TiaImportError, parse_tag_table

        asset = self.asset(asset_id)
        if mode not in ("replace", "merge"):
            raise CloudError(422, "mode must be replace or merge")
        try:
            points, skipped = parse_tag_table(data)
        except TiaImportError as exc:
            raise CloudError(422, str(exc)) from None
        if not points:
            raise CloudError(422, f"No usable tags: all {len(skipped)} rows were skipped ({skipped[0]['reason']})")
        if mode == "merge":  # keep ranges, units and alarms already set up for existing points
            existing = {t["name"]: t for t in self.tags(asset_id)}
            points = [{**existing[p["name"]], "s7": p["s7"]} if p["name"] in existing else p for p in points]
        summary = self._apply_import(asset_id, points, mode, user, "TIA")
        if asset["plc"]["protocol"] != "s7":
            summary["note"] = "This asset's PLC connection isn't set to Siemens S7 yet."
        return {**summary, "skipped": skipped}

    def _apply_import(self, asset_id: str, imported: list[dict[str, Any]], mode: str, user: str,
                      source: str) -> dict[str, Any]:
        seen = {t["name"] for t in imported}
        current = self.tags(asset_id)
        names = {t["name"] for t in current}
        if mode == "replace":
            result = imported
        else:
            by_name = {t["name"]: t for t in current}
            for tag in imported:
                by_name[tag["name"]] = tag
            result = list(by_name.values())
        try:
            validate_asset_io(result, self.asset(asset_id).get("mode_tag"))
        except TagError as exc:
            raise CloudError(422, f"Import rejected – nothing was changed: {exc}") from None
        self._replace_tags(asset_id, result)
        summary = {"added": len(seen - names), "updated": len(seen & names),
                   "removed": len(names - seen) if mode == "replace" else 0, "total": len(result)}
        self.audit(user, "tag.import", asset_id,
                   f"{source} {mode}: +{summary['added']} ~{summary['updated']} -{summary['removed']}")
        return summary

    # -- users ---------------------------------------------------------------------
    def list_users(self, client_id: str | None = None, staff: bool = False) -> list[dict[str, Any]]:
        if staff:
            rows = self.db.q("SELECT * FROM users WHERE kind = 'staff' ORDER BY username")
        elif client_id:
            rows = self.db.q("SELECT * FROM users WHERE client_id = ? ORDER BY username", (client_id,))
        else:
            rows = self.db.q("SELECT * FROM users ORDER BY kind DESC, username")
        for row in rows:
            row.pop("password_hash")
            row["access"] = self.db.q("""SELECT sa.site_id, sa.role, s.name AS site_name FROM site_access sa
                                         JOIN sites s ON s.id = sa.site_id WHERE sa.username = ? ORDER BY s.name""",
                                      (row["username"],))
        return rows

    def user_row(self, username: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None:
            raise CloudError(404, f"Unknown user {username!r}")
        return row

    def create_user(self, data: dict[str, Any], user: str) -> dict[str, Any]:
        username = str(data.get("username") or "").strip().lower()
        if not USERNAME_RE.match(username):
            raise CloudError(422, "Username must be 2–32 characters: lowercase letters, digits, . _ -")
        if self.db.one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise CloudError(422, f"User {username!r} already exists")
        password = str(data.get("password") or "")
        if len(password) < MIN_PASSWORD:
            raise CloudError(422, f"Password must be at least {MIN_PASSWORD} characters")
        kind = data.get("kind")
        email = str(data.get("email") or "").strip()
        if email and not EMAIL_RE.match(email):
            raise CloudError(422, "That email address doesn't look right")
        if kind == "staff":
            role = data.get("portal_role")
            if role not in PORTAL_ROLES:
                raise CloudError(422, f"Staff role must be one of {', '.join(PORTAL_ROLES)}")
            client_id = None
        elif kind == "client":
            role = None
            client_id = data.get("client_id")
            self.client(client_id or "")
        else:
            raise CloudError(422, "kind must be staff or client")
        self.db.run("""INSERT INTO users (username, full_name, email, kind, portal_role, client_id, password_hash,
                       must_change_password, disabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)""",
                    (username, str(data.get("full_name") or "").strip(), email, kind, role, client_id,
                     hash_password(password), _now()))
        self.audit(user, "user.create", username, f"{kind}{' ' + role if role else ''}{' of ' + client_id if client_id else ''}")
        return next(u for u in self.list_users() if u["username"] == username)

    def update_user(self, username: str, data: dict[str, Any], user: str) -> dict[str, Any]:
        target = self.user_row(username)
        changes = []
        with self.db.transaction():
            if data.get("full_name") is not None:
                self.db._db.execute("UPDATE users SET full_name = ? WHERE username = ?",
                                    (str(data["full_name"]).strip(), username))
                changes.append("name")
            if data.get("email") is not None:
                email = str(data["email"]).strip()
                if email and not EMAIL_RE.match(email):
                    raise CloudError(422, "That email address doesn't look right")
                self.db._db.execute("UPDATE users SET email = ? WHERE username = ?", (email, username))
                changes.append("email")
            if data.get("portal_role") is not None:
                if target["kind"] != "staff" or data["portal_role"] not in PORTAL_ROLES:
                    raise CloudError(422, f"Staff role must be one of {', '.join(PORTAL_ROLES)}")
                if target["portal_role"] == "admin" and data["portal_role"] != "admin":
                    self._ensure_other_admin(username)
                self.db._db.execute("UPDATE users SET portal_role = ? WHERE username = ?",
                                    (data["portal_role"], username))
                changes.append(f"role → {data['portal_role']}")
            if data.get("disabled") is not None:
                if data["disabled"] and target["portal_role"] == "admin":
                    self._ensure_other_admin(username)
                self.db._db.execute("UPDATE users SET disabled = ? WHERE username = ?", (int(bool(data["disabled"])), username))
                changes.append("disabled" if data["disabled"] else "enabled")
            if data.get("password") is not None:
                if len(data["password"]) < MIN_PASSWORD:
                    raise CloudError(422, f"Password must be at least {MIN_PASSWORD} characters")
                self.db._db.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE username = ?",
                                    (hash_password(data["password"]), username))
                changes.append("password reset")
        self.audit(user, "user.update", username, ", ".join(changes))
        return next(u for u in self.list_users() if u["username"] == username)

    def _ensure_other_admin(self, username: str) -> None:
        if not self.db.one("SELECT 1 FROM users WHERE portal_role = 'admin' AND disabled = 0 AND username != ?",
                           (username,)):
            raise CloudError(422, "At least one active admin account is required")

    def delete_user(self, username: str, user: str) -> None:
        target = self.user_row(username)
        if target["portal_role"] == "admin":
            self._ensure_other_admin(username)
        with self.db.transaction():
            self.db._db.execute("DELETE FROM site_access WHERE username = ?", (username,))
            self.db._db.execute("DELETE FROM users WHERE username = ?", (username,))
        self.audit(user, "user.delete", username)

    def set_access(self, username: str, access: list[dict[str, Any]], user: str) -> list[dict[str, Any]]:
        """Which sites a client user can sign in to, and their HMI role at each."""
        target = self.user_row(username)
        if target["kind"] != "client":
            raise CloudError(422, "Staff accounts are integrators at every site automatically")
        cleaned = {}
        for entry in access:
            site = self.site(str(entry.get("site_id")))
            if site["client_id"] != target["client_id"]:
                raise CloudError(422, f"Site {site['id']} belongs to another client")
            if entry.get("role") not in HMI_ROLES or entry["role"] == "integrator":
                raise CloudError(422, "Site role must be viewer, operator or engineer")
            cleaned[site["id"]] = entry["role"]
        with self.db.transaction():
            self.db._db.execute("DELETE FROM site_access WHERE username = ?", (username,))
            for site_id, role in cleaned.items():
                self.db._db.execute("INSERT INTO site_access (username, site_id, role) VALUES (?, ?, ?)",
                                    (username, site_id, role))
        self.audit(user, "user.access", username, ", ".join(f"{s}:{r}" for s, r in cleaned.items()) or "none")
        return next(u for u in self.list_users() if u["username"] == username)["access"]

    # -- packages ----------------------------------------------------------------
    def build_package(self, device_id: str) -> tuple[dict[str, Any] | None, list[str]]:
        """Assemble what this device should run. Returns (package, problems)."""
        device = self.device(device_id)
        problems: list[str] = []
        if not device["site_id"]:
            return None, ["Assign the device to a site"]
        site = self.site(device["site_id"])
        client = self.client(device["client_id"])
        assets = self.db.q("SELECT * FROM assets WHERE device_id = ? ORDER BY name", (device_id,))
        if not assets:
            problems.append("Assign at least one asset to this device")
        users = []
        for row in self.db.q("SELECT * FROM users WHERE kind = 'staff' ORDER BY username"):
            users.append(self._edge_user(row, "integrator"))  # your technicians: recovery/commissioning access
        for row in self.db.q("""SELECT u.*, sa.role AS site_role FROM users u JOIN site_access sa ON sa.username = u.username
                                WHERE sa.site_id = ? ORDER BY u.username""", (site["id"],)):
            users.append(self._edge_user(row, row["site_role"]))
        pkg = {
            "format": FORMAT, "schema": SCHEMA,
            "client": {"id": client["id"], "name": client["name"]},
            "site": {"id": site["id"], "name": site["name"], "address": site["address"], "timezone": site["timezone"]},
            "device": {"id": device["id"], "name": device["name"], "serial": device["serial"]},
            "settings": {"history_days": site["history_days"]},
            "assets": [{"id": a["id"], "name": a["name"], "description": a["description"], "screen": a["screen"],
                        "plc": json.loads(a["plc"]), "tags": self.tags(a["id"]),
                        **({"mode_tag": a["mode_tag"]} if a.get("mode_tag") else {})} for a in assets],
            "users": users,
        }
        for asset in pkg["assets"]:
            if not asset["tags"]:
                problems.append(f"{asset['name']} has no I/O defined")
        if not problems:
            try:
                validate_package(json.loads(json.dumps(pkg)))
            except PackageError as exc:
                problems.append(str(exc))
        return pkg, problems

    @staticmethod
    def _edge_user(row: dict[str, Any], role: str) -> dict[str, Any]:
        return {"username": row["username"], "role": role, "password_hash": row["password_hash"],
                "full_name": row["full_name"], "must_change_password": bool(row["must_change_password"]),
                "disabled": bool(row["disabled"]), "created_at": row["created_at"], "last_login": None}

    @staticmethod
    def _comparable(pkg: dict[str, Any]) -> str:
        body = {k: v for k, v in pkg.items() if k not in ("package_version", "published_at", "published_by")}
        return json.dumps(body, sort_keys=True)

    def has_unpublished_changes(self, device_id: str, last: dict[str, Any] | None = None) -> bool:
        latest = self.db.one("SELECT content FROM packages WHERE device_id = ? ORDER BY version DESC LIMIT 1",
                             (device_id,))
        pkg, _ = self.build_package(device_id)
        if pkg is None:
            return False
        if latest is None:
            return bool(pkg["assets"])
        return self._comparable(json.loads(latest["content"])) != self._comparable(pkg)

    def publish(self, device_id: str, user: str, notes: str = "") -> dict[str, Any]:
        pkg, problems = self.build_package(device_id)
        if problems:
            raise CloudError(422, "Can't publish yet: " + "; ".join(problems))
        last = self.db.one("SELECT MAX(version) AS v FROM packages WHERE device_id = ?", (device_id,))
        version = (last["v"] or 0) + 1
        pkg.update(package_version=version, published_at=_now(), published_by=user)
        self.db.run("INSERT INTO packages (device_id, version, created_at, created_by, notes, content) VALUES (?, ?, ?, ?, ?, ?)",
                    (device_id, version, pkg["published_at"], user, notes.strip()[:500], json.dumps(pkg)))
        self.audit(user, "device.publish", device_id, f"v{version}{' – ' + notes.strip() if notes.strip() else ''}")
        return {"version": version, "published_at": pkg["published_at"]}

    def package(self, device_id: str, version: int) -> dict[str, Any]:
        row = self.db.one("SELECT content FROM packages WHERE device_id = ? AND version = ?", (device_id, version))
        if row is None:
            raise CloudError(404, f"No package v{version} for {device_id}")
        return json.loads(row["content"])

    # -- dashboard ---------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        counts = {t: self.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
                  for t in ("clients", "sites", "devices", "assets")}
        devices = [self._device_summary(d) for d in self.db.q("SELECT * FROM devices ORDER BY name")]
        return {"counts": counts,
                "pending": [{"id": d["id"], "name": d["name"], "client_id": d["client_id"]}
                            for d in devices if d["unpublished_changes"]],
                "recent": self.audit_log(12)}
