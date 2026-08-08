"""
Simple file-backed store for SAN switch connection profiles.
Stored in ~/.powerpilot/san_switches.json
Mirrors the pattern used by hmc_store.HMCStore.
"""

import json
import uuid
import logging
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_DIR = Path.home() / ".powerpilot"
STORE_FILE = STORE_DIR / "san_switches.json"


class SANStore:
    def __init__(self):
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        if not STORE_FILE.exists():
            STORE_FILE.write_text("[]")

    def _load(self) -> List[Dict]:
        try:
            return json.loads(STORE_FILE.read_text())
        except Exception:
            return []

    def _save(self, data: List[Dict]):
        STORE_FILE.write_text(json.dumps(data, indent=2))

    def list(self) -> List[Dict]:
        """Return switches without secrets."""
        result = []
        for item in self._load():
            row = {k: v for k, v in item.items() if k != "password"}
            result.append(row)
        return result

    def get(self, switch_id: str) -> Optional[Dict]:
        for item in self._load():
            if item.get("id") == switch_id:
                return item
        return None

    def add(self, data: dict) -> dict:
        items = self._load()
        entry = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", ""),
            "host": data.get("host", ""),
            "vendor": data.get("vendor", "brocade"),   # brocade | cisco
            "model": data.get("model", "auto"),         # auto | modern | legacy (e.g. 24B / FOS 6-7.x)
            "username": data.get("username", "admin"),
            "ssh_port": int(data.get("ssh_port", 22)),
            "auth_method": data.get("auth_method", "password"),  # password | ssh_key
            "key_path": data.get("key_path", ""),
            "password": data.get("password", ""),
            "notes": data.get("notes", ""),
        }
        items.append(entry)
        self._save(items)
        return {k: v for k, v in entry.items() if k != "password"}

    def update(self, switch_id: str, updates: dict):
        items = self._load()
        for item in items:
            if item.get("id") == switch_id:
                item.update(updates)
        self._save(items)

    def remove(self, switch_id: str):
        items = [i for i in self._load() if i.get("id") != switch_id]
        self._save(items)
