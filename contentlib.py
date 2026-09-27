"""Content library: versioned Mesh schemas, entry revisions, rollback, diff.

Storage: SQLite via stdlib sqlite3. Single process, no concurrency.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional
import json

SHORT_TEXT_MAX = 256
LONG_TEXT_MAX = 10000
MAX_REVISIONS_PER_ENTRY = 25

FIELD_TYPES = ("short-text", "long-text", "number", "boolean", "link")


class ContentLibError(Exception):
    """Raised for any failed operation. Failed ops leave no trace."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE meshes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE schema_versions (
                id INTEGER PRIMARY KEY,
                mesh_id INTEGER NOT NULL REFERENCES meshes(id),
                version INTEGER NOT NULL,
                UNIQUE (mesh_id, version)
            );
            CREATE TABLE fields (
                id INTEGER PRIMARY KEY,
                schema_version_id INTEGER NOT NULL REFERENCES schema_versions(id),
                field_uid INTEGER NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                required INTEGER NOT NULL,
                default_json TEXT,
                link_mesh_id INTEGER REFERENCES meshes(id),
                multi INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY,
                mesh_id INTEGER NOT NULL REFERENCES meshes(id)
            );
            CREATE TABLE revisions (
                id INTEGER PRIMARY KEY,
                entry_id INTEGER NOT NULL REFERENCES entries(id),
                rev_no INTEGER NOT NULL,
                schema_version_id INTEGER NOT NULL REFERENCES schema_versions(id),
                values_json TEXT NOT NULL,
                UNIQUE (entry_id, rev_no)
            );
            """
        )
        self.db.commit()

    # --- internal helpers ---
    def _mesh_id(self, name: str) -> int:
        row = self.db.execute("SELECT id FROM meshes WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ContentLibError(f"unknown mesh: {name}")
        return row["id"]

    def _current_schema_version_id(self, mesh_id: int) -> int:
        row = self.db.execute(
            "SELECT id FROM schema_versions WHERE mesh_id = ? ORDER BY version DESC LIMIT 1",
            (mesh_id,),
        ).fetchone()
        return row["id"]

    def _fields_of(self, schema_version_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM fields WHERE schema_version_id = ? ORDER BY id",
            (schema_version_id,),
        ).fetchall()
        return [
            {
                "field_uid": r["field_uid"],
                "name": r["name"],
                "type": r["type"],
                "required": bool(r["required"]),
                "default": json.loads(r["default_json"]) if r["default_json"] is not None else None,
                "link_mesh_id": r["link_mesh_id"],
                "multi": bool(r["multi"]),
            }
            for r in rows
        ]

    def _next_field_uid(self, mesh_id: int) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(f.field_uid), 0) + 1 AS nxt "
            "FROM fields f JOIN schema_versions sv ON f.schema_version_id = sv.id "
            "WHERE sv.mesh_id = ?",
            (mesh_id,),
        ).fetchone()
        return row["nxt"]

    def _validate_default(self, mesh_id: int, spec: dict) -> None:
        ftype = spec.get("type")
        if ftype not in FIELD_TYPES:
            raise ContentLibError(f"unknown field type: {ftype}")
        if ftype == "link":
            if "link" not in spec:
                raise ContentLibError("link field requires 'link' target mesh")
            spec["link_mesh_id"] = self._mesh_id(spec["link"])
        else:
            spec["link_mesh_id"] = None
        if spec.get("required") and spec.get("default") is None:
            raise ContentLibError(
                f"required field '{spec['name']}' must have a default", field=spec["name"]
            )
        default = spec.get("default")
        if default is not None:
            self._check_value(spec, default, spec["name"])

    def _check_value(self, field: dict, value: Any, name: str) -> None:
        ftype = field["type"]
        if ftype == "short-text":
            if not isinstance(value, str) or len(value) > SHORT_TEXT_MAX:
                raise ContentLibError(f"field '{name}': invalid short-text", field=name)
        elif ftype == "long-text":
            if not isinstance(value, str) or len(value) > LONG_TEXT_MAX:
                raise ContentLibError(f"field '{name}': invalid long-text", field=name)
        elif ftype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ContentLibError(f"field '{name}': invalid number", field=name)
        elif ftype == "boolean":
            if not isinstance(value, bool):
                raise ContentLibError(f"field '{name}': invalid boolean", field=name)
        elif ftype == "link":
            targets = value if field.get("multi") else [value]
            if not isinstance(targets, list):
                raise ContentLibError(f"field '{name}': invalid link", field=name)
            for target in targets:
                row = self.db.execute(
                    "SELECT mesh_id FROM entries WHERE id = ?", (target,)
                ).fetchone()
                if row is None or row["mesh_id"] != field["link_mesh_id"]:
                    raise ContentLibError(
                        f"field '{name}': link target {target} missing", field=name
                    )

    def _insert_schema_version(self, mesh_id: int, fields: list[dict]) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM schema_versions WHERE mesh_id = ?",
            (mesh_id,),
        ).fetchone()
        version = row["v"]
        cur = self.db.execute(
            "INSERT INTO schema_versions (mesh_id, version) VALUES (?, ?)",
            (mesh_id, version),
        )
        sv_id = cur.lastrowid
        for f in fields:
            self.db.execute(
                "INSERT INTO fields (schema_version_id, field_uid, name, type, required,"
                " default_json, link_mesh_id, multi) VALUES (?,?,?,?,?,?,?,?)",
                (
                    sv_id,
                    f["field_uid"],
                    f["name"],
                    f["type"],
                    1 if f["required"] else 0,
                    json.dumps(f.get("default")) if f.get("default") is not None else None,
                    f.get("link_mesh_id"),
                    1 if f.get("multi") else 0,
                ),
            )
        return version

    # --- mesh / schema ---
    def create_mesh(self, name: str, fields: list[dict]) -> int:
        specs = []
        seen = set()
        for i, f in enumerate(fields):
            spec = {
                "field_uid": i + 1,
                "name": f["name"],
                "type": f["type"],
                "required": bool(f.get("required", False)),
                "default": f.get("default"),
                "multi": bool(f.get("multi", False)),
            }
            if "link" in f:
                spec["link"] = f["link"]
            if spec["name"] in seen:
                raise ContentLibError(f"duplicate field name: {spec['name']}")
            seen.add(spec["name"])
            specs.append(spec)
        try:
            with self.db:
                cur = self.db.execute("INSERT INTO meshes (name) VALUES (?)", (name,))
                mesh_id = cur.lastrowid
                for spec in specs:
                    self._validate_default(mesh_id, spec)
                self._insert_schema_version(mesh_id, specs)
            return mesh_id
        except sqlite3.IntegrityError as e:
            raise ContentLibError(f"mesh '{name}' already exists") from e

    def change_schema(self, mesh: str, ops: list[dict]) -> int:
        mesh_id = self._mesh_id(mesh)
        sv_id = self._current_schema_version_id(mesh_id)
        fields = [dict(f) for f in self._fields_of(sv_id)]
        next_uid = self._next_field_uid(mesh_id)

        def find(name):
            matches = [f for f in fields if f["name"] == name]
            if not matches:
                raise ContentLibError(f"unknown field: {name}", field=name)
            return matches[0]

        pending_renames = []
        for op in ops:
            kind = op["op"]
            if kind == "add_field":
                if any(f["name"] == op["name"] for f in fields):
                    raise ContentLibError(f"field already exists: {op['name']}", field=op["name"])
                spec = {
                    "field_uid": next_uid,
                    "name": op["name"],
                    "type": op["type"],
                    "required": bool(op.get("required", False)),
                    "default": op.get("default"),
                    "multi": bool(op.get("multi", False)),
                }
                if "link" in op:
                    spec["link"] = op["link"]
                self._validate_default(mesh_id, spec)
                fields.append(spec)
                next_uid += 1
            elif kind == "remove_field":
                fields.remove(find(op["name"]))
            elif kind == "rename_field":
                target = find(op["from"])
                target["name"] = f"__renaming_{target['field_uid']}"
                pending_renames.append((target, op["to"]))
            elif kind == "convert_field":
                target = find(op["name"])
                to = op["to"]
                pair = {target["type"], to}
                if pair != {"short-text", "long-text"}:
                    raise ContentLibError(
                        f"cannot convert '{op['name']}' to {to}", field=op["name"]
                    )
                if to == "short-text":
                    self._check_short_text_convertible(mesh_id, target)
                target["type"] = to
            else:
                raise ContentLibError(f"unknown schema op: {kind}")

        for target, new_name in pending_renames:
            target["name"] = new_name
        names = [f["name"] for f in fields]
        if len(names) != len(set(names)):
            raise ContentLibError("duplicate field names after change")
        with self.db:
            return self._insert_schema_version(mesh_id, fields)

    def _check_short_text_convertible(self, mesh_id: int, field: dict) -> None:
        uid = str(field["field_uid"])
        rows = self.db.execute(
            "SELECT r.values_json FROM revisions r "
            "JOIN entries e ON r.entry_id = e.id "
            "WHERE e.mesh_id = ? AND r.rev_no = ("
            "  SELECT MAX(rev_no) FROM revisions WHERE entry_id = e.id"
            ")",
            (mesh_id,),
        ).fetchall()
        for row in rows:
            values = json.loads(row["values_json"])
            value = values.get(uid)
            if isinstance(value, str) and len(value) > SHORT_TEXT_MAX:
                raise ContentLibError(
                    f"field '{field['name']}': existing value exceeds short-text limit",
                    field=field["name"],
                )

    # --- entries / revisions ---
    def save_entry(self, mesh: str, values: dict, entry: Optional[int] = None) -> int:
        mesh_id = self._mesh_id(mesh)
        sv_id = self._current_schema_version_id(mesh_id)
        fields = self._fields_of(sv_id)
        by_name = {f["name"]: f for f in fields}

        if entry is None:
            base = {}
        else:
            row = self.db.execute(
                "SELECT mesh_id FROM entries WHERE id = ?", (entry,)
            ).fetchone()
            if row is None or row["mesh_id"] != mesh_id:
                raise ContentLibError(f"unknown entry in mesh '{mesh}': {entry}")
            base = self._current_values(entry)

        merged = dict(base)
        for name, value in values.items():
            if name not in by_name:
                raise ContentLibError(f"unknown field: {name}", field=name)
            merged[str(by_name[name]["field_uid"])] = value

        content = self._build_content(fields, merged)
        with self.db:
            if entry is None:
                cur = self.db.execute("INSERT INTO entries (mesh_id) VALUES (?)", (mesh_id,))
                entry = cur.lastrowid
            self._insert_revision(entry, sv_id, content)
        return entry

    def _build_content(self, fields: list[dict], merged: dict) -> dict:
        content = {}
        for f in fields:
            uid = str(f["field_uid"])
            value = merged.get(uid, f["default"])
            if value is None:
                if f["required"]:
                    raise ContentLibError(
                        f"field '{f['name']}': required value missing", field=f["name"]
                    )
            else:
                self._check_value(f, value, f["name"])
            content[uid] = value
        return content

    def _insert_revision(self, entry: int, sv_id: int, content: dict) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(rev_no), 0) + 1 AS n FROM revisions WHERE entry_id = ?",
            (entry,),
        ).fetchone()
        rev_no = row["n"]
        self.db.execute(
            "INSERT INTO revisions (entry_id, rev_no, schema_version_id, values_json)"
            " VALUES (?,?,?,?)",
            (entry, rev_no, sv_id, json.dumps(content)),
        )
        self.db.execute(
            "DELETE FROM revisions WHERE entry_id = ? AND rev_no <= ?",
            (entry, rev_no - MAX_REVISIONS_PER_ENTRY),
        )
        return rev_no

    def _current_values(self, entry: int) -> dict:
        row = self.db.execute(
            "SELECT values_json FROM revisions WHERE entry_id = ? ORDER BY rev_no DESC LIMIT 1",
            (entry,),
        ).fetchone()
        if row is None:
            return {}
        return json.loads(row["values_json"])

    def delete_entry(self, entry: int) -> None:
        row = self.db.execute("SELECT mesh_id FROM entries WHERE id = ?", (entry,)).fetchone()
        if row is None:
            raise ContentLibError(f"unknown entry: {entry}")
        for other in self.db.execute("SELECT id, mesh_id FROM entries WHERE id != ?", (entry,)):
            sv_id = self._current_schema_version_id(other["mesh_id"])
            fields = self._fields_of(sv_id)
            values = self._current_values(other["id"])
            for f in fields:
                if f["type"] != "link":
                    continue
                value = values.get(str(f["field_uid"]), f["default"])
                targets = value if f["multi"] and value is not None else [value]
                if entry in targets:
                    raise ContentLibError(
                        f"entry {entry} still referenced by entry {other['id']}"
                        f" via field '{f['name']}'",
                        field=f["name"],
                    )
        with self.db:
            self.db.execute("DELETE FROM revisions WHERE entry_id = ?", (entry,))
            self.db.execute("DELETE FROM entries WHERE id = ?", (entry,))

    def get_entry(self, entry: int) -> dict:
        row = self.db.execute("SELECT mesh_id FROM entries WHERE id = ?", (entry,)).fetchone()
        if row is None:
            raise ContentLibError(f"unknown entry: {entry}")
        fields = self._fields_of(self._current_schema_version_id(row["mesh_id"]))
        values = self._current_values(entry)
        return {
            f["name"]: values.get(str(f["field_uid"]), f["default"]) for f in fields
        }

    def latest_rev_no(self, entry: int) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(rev_no), 0) AS n FROM revisions WHERE entry_id = ?",
            (entry,),
        ).fetchone()
        return row["n"]

    def get_revision(self, entry: int, rev: int) -> dict:
        row = self.db.execute(
            "SELECT schema_version_id, values_json FROM revisions WHERE entry_id = ? AND rev_no = ?",
            (entry, rev),
        ).fetchone()
        if row is None:
            raise ContentLibError(f"unknown revision {rev} for entry {entry}")
        sv_row = self.db.execute(
            "SELECT version FROM schema_versions WHERE id = ?", (row["schema_version_id"],)
        ).fetchone()
        fields = self._fields_of(row["schema_version_id"])
        names = {str(f["field_uid"]): f["name"] for f in fields}
        values = json.loads(row["values_json"])
        return {
            "schema_version": sv_row["version"],
            "values": {names[uid]: value for uid, value in values.items() if uid in names},
        }

    def rollback(self, entry: int, rev: int) -> int:
        row = self.db.execute(
            "SELECT e.mesh_id, r.values_json FROM entries e "
            "JOIN revisions r ON r.entry_id = e.id "
            "WHERE e.id = ? AND r.rev_no = ?",
            (entry, rev),
        ).fetchone()
        if row is None:
            raise ContentLibError(f"unknown revision {rev} for entry {entry}")
        sv_id = self._current_schema_version_id(row["mesh_id"])
        fields = self._fields_of(sv_id)
        old_values = json.loads(row["values_json"])
        content = self._build_content(fields, old_values)
        with self.db:
            return self._insert_revision(entry, sv_id, content)

    def diff(self, entry: int, rev_a: int, rev_b: int) -> dict:
        def load(rev):
            row = self.db.execute(
                "SELECT schema_version_id, values_json FROM revisions"
                " WHERE entry_id = ? AND rev_no = ?",
                (entry, rev),
            ).fetchone()
            if row is None:
                raise ContentLibError(f"unknown revision {rev} for entry {entry}")
            fields = self._fields_of(row["schema_version_id"])
            names = {str(f["field_uid"]): f["name"] for f in fields}
            return names, json.loads(row["values_json"])

        names_a, values_a = load(rev_a)
        names_b, values_b = load(rev_b)
        added, removed, changed = {}, {}, {}
        for uid in values_a.keys() | values_b.keys():
            in_a, in_b = uid in values_a, uid in values_b
            if in_a and in_b:
                if values_a[uid] != values_b[uid]:
                    changed[names_b.get(uid, uid)] = (values_a[uid], values_b[uid])
            elif in_a:
                removed[names_a.get(uid, uid)] = values_a[uid]
            else:
                added[names_b.get(uid, uid)] = values_b[uid]
        return {"added": added, "removed": removed, "changed": changed}
