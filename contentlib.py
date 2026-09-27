"""内容库：Mesh schema 版本化、条目修订与回滚。

存储：SQLite（标准库 sqlite3）。单进程、无并发。
"""

from __future__ import annotations

import json
import sqlite3
import uuid

SHORT_TEXT_MAX = 256
LONG_TEXT_MAX = 10000
MAX_REVISIONS = 25

FIELD_TYPES = ("short-text", "long-text", "number", "boolean", "link")


class MeshError(Exception):
    """所有内容库操作失败的基类。"""


class ValidationError(MeshError):
    """内容在当前 schema 下校验失败。`field` 指明出问题的字段名。"""

    def __init__(self, field: str, message: str):
        super().__init__(f"field {field!r}: {message}")
        self.field = field


class Store:
    """一个内容库实例，绑定一个 SQLite 数据库。"""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE meshes(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                version INTEGER NOT NULL
            );
            CREATE TABLE schema_versions(
                mesh_id INTEGER NOT NULL REFERENCES meshes(id),
                version INTEGER NOT NULL,
                snapshot TEXT NOT NULL,
                PRIMARY KEY(mesh_id, version)
            );
            CREATE TABLE entries(
                id INTEGER PRIMARY KEY,
                mesh_id INTEGER NOT NULL REFERENCES meshes(id),
                deleted INTEGER NOT NULL DEFAULT 0,
                rev_counter INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE revisions(
                entry_id INTEGER NOT NULL REFERENCES entries(id),
                rev_no INTEGER NOT NULL,
                schema_version INTEGER NOT NULL,
                values_json TEXT NOT NULL,
                PRIMARY KEY(entry_id, rev_no)
            );
        """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- Mesh / schema ----

    def create_mesh(self, name: str, fields: list[dict]) -> int:
        """创建内容类型，返回 mesh_id。fields 为字段定义 dict 列表。"""
        norm = [self._normalize_field(f) for f in fields]
        self._check_field_defs(norm)
        cur = self._conn.execute("INSERT INTO meshes(name, version) VALUES (?, 1)", (name,))
        mesh_id = cur.lastrowid
        self._put_snapshot(mesh_id, 1, norm)
        self._conn.commit()
        return mesh_id

    def change_schema(self, mesh_id: int, op: dict) -> int:
        """应用一次 schema 变更，返回新的 schema 版本号。"""
        version = self._current_version(mesh_id)
        fields = [dict(f) for f in self._snapshot(mesh_id, version)]
        by_name = {f["name"]: f for f in fields}
        kind = op.get("op")

        if kind == "add_field":
            f = self._normalize_field(op["field"])
            if f["name"] in by_name:
                raise MeshError(f"field {f['name']!r} already exists")
            if f["required"] and f["default"] is None:
                raise MeshError(f"required field {f['name']!r} must have a default")
            self._check_field_defs([f])
            fields.append(f)
        elif kind == "delete_field":
            if op["name"] not in by_name:
                raise MeshError(f"no such field {op['name']!r}")
            fields = [f for f in fields if f["name"] != op["name"]]
        elif kind == "rename_field":
            old, new = op["from"], op["to"]
            if old not in by_name:
                raise MeshError(f"no such field {old!r}")
            if new in by_name:
                raise MeshError(f"field {new!r} already exists")
            by_name[old]["name"] = new
        elif kind == "convert_type":
            f = by_name.get(op["name"])
            if f is None:
                raise MeshError(f"no such field {op['name']!r}")
            to = op["to"]
            if {f["type"], to} != {"short-text", "long-text"}:
                raise MeshError("only short-text <-> long-text conversion is allowed")
            if to == "short-text":
                # 任一条目当前值超长则拒绝
                for eid, values_json in self._conn.execute(
                        "SELECT e.id, r.values_json FROM entries e "
                        "JOIN revisions r ON r.entry_id = e.id AND r.rev_no = e.rev_counter "
                        "WHERE e.mesh_id = ? AND e.deleted = 0", (mesh_id,)):
                    value = json.loads(values_json).get(f["id"])
                    if isinstance(value, str) and len(value) > SHORT_TEXT_MAX:
                        raise ValidationError(
                            f["name"], f"entry {eid} value exceeds {SHORT_TEXT_MAX} chars")
            if f["default"] is not None:
                probe = dict(f, type=to)
                self._check_value(probe, f["default"])
            f["type"] = to
        else:
            raise MeshError(f"unknown schema op {kind!r}")

        new_version = version + 1
        self._put_snapshot(mesh_id, new_version, fields)
        self._conn.execute("UPDATE meshes SET version = ? WHERE id = ?", (new_version, mesh_id))
        self._conn.commit()
        return new_version

    # ---- 内部：schema 快照 ----

    def _normalize_field(self, f: dict) -> dict:
        return {
            "id": f.get("id") or uuid.uuid4().hex,
            "name": f["name"],
            "type": f["type"],
            "required": bool(f.get("required", False)),
            "default": f.get("default"),
            "link_mesh": f.get("link_mesh"),
            "multi": bool(f.get("multi", False)),
        }

    def _check_field_defs(self, fields: list[dict]) -> None:
        names = set()
        for f in fields:
            if f["name"] in names:
                raise MeshError(f"duplicate field name {f['name']!r}")
            names.add(f["name"])
            if f["type"] not in FIELD_TYPES:
                raise MeshError(f"unknown field type {f['type']!r}")
            if f["type"] == "link":
                if not f["link_mesh"]:
                    raise MeshError(f"link field {f['name']!r} missing link_mesh")
                self._mesh_id(f["link_mesh"])  # 必须存在
            elif f["link_mesh"] is not None:
                raise MeshError(f"non-link field {f['name']!r} has link_mesh")
            if f["default"] is not None:
                self._check_value(f, f["default"])

    def _mesh_id(self, mesh) -> int:
        if isinstance(mesh, int):
            row = self._conn.execute("SELECT id FROM meshes WHERE id = ?", (mesh,)).fetchone()
        else:
            row = self._conn.execute("SELECT id FROM meshes WHERE name = ?", (mesh,)).fetchone()
        if not row:
            raise MeshError(f"no such mesh {mesh!r}")
        return row[0]

    def _snapshot(self, mesh_id: int, version: int | None = None) -> list[dict]:
        if version is None:
            version = self._conn.execute(
                "SELECT version FROM meshes WHERE id = ?", (mesh_id,)).fetchone()
            if not version:
                raise MeshError(f"no such mesh {mesh_id!r}")
            version = version[0]
        row = self._conn.execute(
            "SELECT snapshot FROM schema_versions WHERE mesh_id = ? AND version = ?",
            (mesh_id, version)).fetchone()
        if not row:
            raise MeshError(f"no schema version {version} for mesh {mesh_id}")
        return json.loads(row[0])

    def _current_version(self, mesh_id: int) -> int:
        row = self._conn.execute("SELECT version FROM meshes WHERE id = ?", (mesh_id,)).fetchone()
        if not row:
            raise MeshError(f"no such mesh {mesh_id!r}")
        return row[0]

    def _put_snapshot(self, mesh_id: int, version: int, fields: list[dict]) -> None:
        self._conn.execute(
            "INSERT INTO schema_versions(mesh_id, version, snapshot) VALUES (?, ?, ?)",
            (mesh_id, version, json.dumps(fields)))

    def _check_value(self, field: dict, value) -> None:
        """单个值按字段定义校验，失败抛 ValidationError。"""
        name, ftype = field["name"], field["type"]
        if ftype == "short-text":
            if not isinstance(value, str):
                raise ValidationError(name, "expected short-text")
            if len(value) > SHORT_TEXT_MAX:
                raise ValidationError(name, f"short-text exceeds {SHORT_TEXT_MAX} chars")
        elif ftype == "long-text":
            if not isinstance(value, str):
                raise ValidationError(name, "expected long-text")
            if len(value) > LONG_TEXT_MAX:
                raise ValidationError(name, f"long-text exceeds {LONG_TEXT_MAX} chars")
        elif ftype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(name, "expected number")
        elif ftype == "boolean":
            if not isinstance(value, bool):
                raise ValidationError(name, "expected boolean")
        elif ftype == "link":
            values = value if field["multi"] else [value]
            if field["multi"] and not isinstance(value, list):
                raise ValidationError(name, "expected list of links")
            for target in values:
                if not isinstance(target, int):
                    raise ValidationError(name, "link target must be an entry id")
                row = self._conn.execute(
                    "SELECT mesh_id, deleted FROM entries WHERE id = ?", (target,)).fetchone()
                if not row or row[1]:
                    raise ValidationError(name, f"link target {target} does not exist")
                if row[0] != self._mesh_id(field["link_mesh"]):
                    raise ValidationError(name, f"link target {target} belongs to another mesh")

    # ---- 条目与修订 ----

    def save_entry(self, mesh_id: int, values: dict, entry_id: int | None = None) -> tuple[int, int]:
        """按当前字段名写值，产生新修订，返回 (entry_id, rev_no)。"""
        mesh_id = self._mesh_id(mesh_id)
        version = self._current_version(mesh_id)
        snapshot = self._snapshot(mesh_id, version)
        by_name = {f["name"]: f for f in snapshot}
        for name in values:
            if name not in by_name:
                raise MeshError(f"unknown field {name!r} for current schema")
        by_id = {}
        for f in snapshot:
            if f["name"] in values:
                by_id[f["id"]] = values[f["name"]]
            elif f["default"] is not None:
                by_id[f["id"]] = f["default"]
        self._validate_values(snapshot, by_id)

        if entry_id is None:
            cur = self._conn.execute("INSERT INTO entries(mesh_id) VALUES (?)", (mesh_id,))
            entry_id = cur.lastrowid
        else:
            row = self._conn.execute(
                "SELECT mesh_id, deleted FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if not row or row[1]:
                raise MeshError(f"no such entry {entry_id}")
            if row[0] != mesh_id:
                raise MeshError(f"entry {entry_id} belongs to another mesh")
        rev_no = self._insert_revision(entry_id, version, by_id)
        self._conn.commit()
        return entry_id, rev_no

    def delete_entry(self, entry_id: int) -> None:
        """删除条目；仍被任何条目的当前值引用时拒绝。"""
        row = self._conn.execute(
            "SELECT mesh_id, deleted FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row or row[1]:
            raise MeshError(f"no such entry {entry_id}")
        # 扫描所有存活条目的当前值（按各自 Mesh 当前 schema 的 link 字段）
        for eid, mesh_id, values_json in self._conn.execute(
                "SELECT e.id, e.mesh_id, r.values_json FROM entries e "
                "JOIN revisions r ON r.entry_id = e.id AND r.rev_no = e.rev_counter "
                "WHERE e.deleted = 0"):
            snapshot = self._snapshot(mesh_id)
            values = json.loads(values_json)
            for f in snapshot:
                if f["type"] != "link":
                    continue
                v = values.get(f["id"])
                targets = v if isinstance(v, list) else ([v] if v is not None else [])
                if entry_id in targets:
                    raise MeshError(f"entry {entry_id} is still referenced by entry {eid}")
        self._conn.execute("UPDATE entries SET deleted = 1 WHERE id = ?", (entry_id,))
        self._conn.commit()

    def get_revision(self, entry_id: int, rev_no: int) -> dict:
        """按写入时的 schema 版本原样返回内容（字段名 -> 值）。"""
        entry_id, rev_no, values, snapshot = self._load_revision(entry_id, rev_no)
        names = {f["id"]: f["name"] for f in snapshot}
        return {names[fid]: v for fid, v in values.items()}

    def rollback(self, entry_id: int, rev_no: int) -> int:
        """回滚到指定修订，本身产生一条新修订，返回新 rev_no。"""
        row = self._conn.execute(
            "SELECT mesh_id, deleted FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row or row[1]:
            raise MeshError(f"no such entry {entry_id}")
        mesh_id = row[0]
        _, _, old_values, _ = self._load_revision(entry_id, rev_no)
        version = self._current_version(mesh_id)
        snapshot = self._snapshot(mesh_id, version)
        # 当前字段：目标修订里有同身份字段的值则取之，否则取默认值；
        # 已删除字段的旧值直接丢弃（哪怕现在有同名新字段）。
        by_id = {}
        for f in snapshot:
            if f["id"] in old_values:
                by_id[f["id"]] = old_values[f["id"]]
            elif f["default"] is not None:
                by_id[f["id"]] = f["default"]
        self._validate_values(snapshot, by_id)  # 失败则整体失败，不落盘
        new_rev = self._insert_revision(entry_id, version, by_id)
        self._conn.commit()
        return new_rev

    def diff(self, entry_id: int, rev_a: int, rev_b: int) -> dict:
        """按字段身份对比两个修订，返回 {"added": ..., "removed": ..., "changed": ...}。"""
        _, _, values_a, snap_a = self._load_revision(entry_id, rev_a)
        _, _, values_b, snap_b = self._load_revision(entry_id, rev_b)
        names_a = {f["id"]: f["name"] for f in snap_a}
        names_b = {f["id"]: f["name"] for f in snap_b}
        added, removed, changed = {}, {}, {}
        for fid, v in values_b.items():
            if fid not in values_a:
                added[names_b[fid]] = v
            elif values_a[fid] != v:
                changed[names_b[fid]] = (values_a[fid], v)
        for fid, v in values_a.items():
            if fid not in values_b:
                removed[names_a[fid]] = v
        return {"added": added, "removed": removed, "changed": changed}

    def _validate_values(self, snapshot: list[dict], by_id: dict) -> None:
        for f in snapshot:
            value = by_id.get(f["id"])
            if value is None:
                if f["required"]:
                    raise ValidationError(f["name"], "required field missing")
                continue
            self._check_value(f, value)

    def _insert_revision(self, entry_id: int, version: int, by_id: dict) -> int:
        row = self._conn.execute(
            "SELECT rev_counter FROM entries WHERE id = ?", (entry_id,)).fetchone()
        rev_no = row[0] + 1
        self._conn.execute(
            "INSERT INTO revisions(entry_id, rev_no, schema_version, values_json) VALUES (?, ?, ?, ?)",
            (entry_id, rev_no, version, json.dumps(by_id)))
        self._conn.execute("UPDATE entries SET rev_counter = ? WHERE id = ?", (rev_no, entry_id))
        self._conn.execute("""
            DELETE FROM revisions WHERE entry_id = ? AND rev_no NOT IN (
                SELECT rev_no FROM revisions WHERE entry_id = ?
                ORDER BY rev_no DESC LIMIT ?
            )""", (entry_id, entry_id, MAX_REVISIONS))
        return rev_no

    # ---- 辅助 ----

    def current(self, entry_id: int) -> dict:
        """条目当前内容（字段名 -> 值，按当前 schema）。"""
        row = self._conn.execute(
            "SELECT mesh_id, deleted, rev_counter FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row or row[1]:
            raise MeshError(f"no such entry {entry_id}")
        mesh_id, _, rev_no = row
        snapshot = self._snapshot(mesh_id)
        r = self._conn.execute(
            "SELECT values_json FROM revisions WHERE entry_id = ? AND rev_no = ?",
            (entry_id, rev_no)).fetchone()
        values = json.loads(r[0])
        out = {}
        for f in snapshot:
            if f["id"] in values:
                out[f["name"]] = values[f["id"]]
            elif f["default"] is not None:
                out[f["name"]] = f["default"]
            else:
                out[f["name"]] = None
        return out

    def revisions(self, entry_id: int) -> list[int]:
        """条目现存修订号列表（升序）。"""
        return [r[0] for r in self._conn.execute(
            "SELECT rev_no FROM revisions WHERE entry_id = ? ORDER BY rev_no", (entry_id,))]

    def _load_revision(self, entry_id: int, rev_no: int):
        row = self._conn.execute(
            "SELECT e.mesh_id, r.schema_version, r.values_json FROM entries e "
            "JOIN revisions r ON r.entry_id = e.id "
            "WHERE e.id = ? AND r.rev_no = ?", (entry_id, rev_no)).fetchone()
        if not row:
            raise MeshError(f"no revision {rev_no} for entry {entry_id}")
        mesh_id, version, values_json = row
        snapshot = self._snapshot(mesh_id, version)
        return entry_id, rev_no, json.loads(values_json), snapshot
