"""
Simple file-backed store for NIM server & client connection profiles
and their relationships.

Stored in ~/.powerpilot/nim.json

Schema:
{
    "servers": [ {id, name, host, username, ssh_port, auth_method, key_path, password, notes} ],
    "clients": [ {id, name, host, server_id, notes} ]
}

The server_id field on each client establishes the relationship to a NIM server.
"""

import json
import uuid
import logging
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_DIR = Path.home() / ".powerpilot"
STORE_FILE = STORE_DIR / "nim.json"


class NIMStore:
    def __init__(self):
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        if not STORE_FILE.exists():
            STORE_FILE.write_text(json.dumps({"servers": [], "clients": []}, indent=2))

    # ── internal ──────────────────────────────────────────────

    def _load(self) -> Dict:
        try:
            data = json.loads(STORE_FILE.read_text())
            data.setdefault("servers", [])
            data.setdefault("clients", [])
            return data
        except Exception:
            return {"servers": [], "clients": []}

    def _save(self, data: Dict):
        STORE_FILE.write_text(json.dumps(data, indent=2))

    # ── Servers ───────────────────────────────────────────────

    def list_servers(self) -> List[Dict]:
        """Return servers without passwords."""
        result = []
        for item in self._load()["servers"]:
            row = {k: v for k, v in item.items() if k != "password"}
            result.append(row)
        return result

    def get_server(self, server_id: str) -> Optional[Dict]:
        for item in self._load()["servers"]:
            if item.get("id") == server_id:
                return item
        return None

    def add_server(self, data: dict) -> dict:
        store = self._load()
        entry = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", ""),
            "host": data.get("host", ""),
            "username": data.get("username", "root"),
            "ssh_port": int(data.get("ssh_port", 22)),
            "auth_method": data.get("auth_method", "ssh_key"),  # password | ssh_key
            "key_path": data.get("key_path", ""),
            "password": data.get("password", ""),
            "notes": data.get("notes", ""),
        }
        store["servers"].append(entry)
        self._save(store)
        return {k: v for k, v in entry.items() if k != "password"}

    def update_server(self, server_id: str, updates: dict):
        store = self._load()
        for item in store["servers"]:
            if item.get("id") == server_id:
                item.update(updates)
        self._save(store)

    def remove_server(self, server_id: str):
        store = self._load()
        store["servers"] = [s for s in store["servers"] if s.get("id") != server_id]
        # Also remove clients that belong to this server
        store["clients"] = [c for c in store["clients"] if c.get("server_id") != server_id]
        self._save(store)

    # ── Clients ───────────────────────────────────────────────

    def list_clients(self, server_id: str = None) -> List[Dict]:
        """Return clients, optionally filtered by server_id."""
        clients = self._load()["clients"]
        if server_id:
            clients = [c for c in clients if c.get("server_id") == server_id]
        return clients

    def get_client(self, client_id: str) -> Optional[Dict]:
        for item in self._load()["clients"]:
            if item.get("id") == client_id:
                return item
        return None

    def add_client(self, data: dict) -> dict:
        store = self._load()
        entry = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", ""),
            "host": data.get("host", ""),
            "server_id": data.get("server_id", ""),
            "notes": data.get("notes", ""),
        }
        store["clients"].append(entry)
        self._save(store)
        return entry

    def update_client(self, client_id: str, updates: dict):
        store = self._load()
        for item in store["clients"]:
            if item.get("id") == client_id:
                item.update(updates)
        self._save(store)

    def remove_client(self, client_id: str):
        store = self._load()
        store["clients"] = [c for c in store["clients"] if c.get("id") != client_id]
        self._save(store)
