# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from typing import Any

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session


class TraceRepository:
    """Repository handling querying traces and constructing hierarchical trace trees."""

    def __init__(self, db_path=None):
        self.db_path = db_path

    def get_trace_by_id(self, trace_id: str, db_path=None) -> dict[str, Any] | None:
        with db_session(db_path or self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_trace_tree(self, session_id: str) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT trace_id, parent_trace_id, type, name, status, timestamp,"
                " duration, CASE WHEN type IN ('raw_thinking', 'thinking') THEN"
                " payload ELSE NULL END AS payload FROM traces WHERE session_id = ?"
                " ORDER BY timestamp ASC",
                (session_id,),
            )
            rows = cursor.fetchall()

        nodes = {}
        for r in rows:
            row_dict = dict(r)
            if row_dict.get("payload"):
                try:
                    row_dict["payload"] = json.loads(row_dict["payload"])
                except (ValueError, TypeError):
                    # Non-JSON payload: keep the raw string for display.
                    pass
            nodes[row_dict["trace_id"]] = {**row_dict, "children": []}

        root_nodes = []
        for node_id, node in nodes.items():
            parent_id = node["parent_trace_id"]
            if parent_id and parent_id in nodes:
                nodes[parent_id]["children"].append(node)
            else:
                root_nodes.append(node)

        def sanitize_tree(nodes_list):
            sanitized = []
            for node in nodes_list:
                node["children"] = sanitize_tree(node["children"])
                if node["name"] == "node":
                    sanitized.extend(node["children"])
                else:
                    sanitized.append(node)
            return sanitized

        return sanitize_tree(root_nodes)

    def get_step_traces_tree(self, session_id: str, step_id: str) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT trace_id, parent_trace_id, step_id, type, name, status,"
                " timestamp, duration, CASE WHEN type IN ('raw_thinking',"
                " 'thinking') THEN payload ELSE NULL END AS payload FROM traces"
                " WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,),
            )
            rows = cursor.fetchall()

        nodes = {}
        for r in rows:
            row_dict = dict(r)
            if row_dict.get("payload"):
                try:
                    row_dict["payload"] = json.loads(row_dict["payload"])
                except (ValueError, TypeError):
                    # Non-JSON payload: keep the raw string for display.
                    pass
            nodes[row_dict["trace_id"]] = {**row_dict, "children": []}

        step_roots = []
        for n_id, node in nodes.items():
            if node.get("step_id") == step_id:
                step_roots.append(node)

        for n_id, node in nodes.items():
            parent_id = node["parent_trace_id"]
            if parent_id and parent_id in nodes:
                nodes[parent_id]["children"].append(node)

        def sanitize_tree(nodes_list, visited=None):
            if visited is None:
                visited = set()
            sanitized = []
            for node in nodes_list:
                node_id = node.get("trace_id")
                if node_id and node_id in visited:
                    continue
                if node_id:
                    visited.add(node_id)
                node["children"] = sanitize_tree(node["children"], visited.copy())
                if node["name"] == "node":
                    sanitized.extend(node["children"])
                else:
                    sanitized.append(node)
            return sanitized

        return sanitize_tree(step_roots)


trace_repo = TraceRepository()
