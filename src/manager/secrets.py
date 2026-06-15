"""
manager/secrets.py — DPAPI-backed secret store for the manager.

Two scopes stored in registry.db's agent_secrets table:
  agent_id = "__manager__"  →  activation_key, manager_api_token, channel_token
  agent_id = "agent-N"      →  mt5_password

Reuses the same DPAPI pattern as src/infra/db.py.
"""

from __future__ import annotations

import base64
import logging
import sys
import time

from src.manager.registry import AgentRegistry

logger = logging.getLogger(__name__)

_DPAPI_PREFIX = "DPAPI:"
_MANAGER_SCOPE = "__manager__"


def _dpapi_encrypt(plaintext: str) -> str:
    if sys.platform != "win32":
        return plaintext
    import ctypes
    import ctypes.wintypes

    data = plaintext.encode("utf-8")
    blob_in = ctypes.create_string_buffer(data)
    blob_len = ctypes.c_uint(len(data))

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    input_blob  = DATA_BLOB(blob_len, blob_in)
    output_blob = DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob), None, None, None, None, 0,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise RuntimeError("CryptProtectData failed")

    encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    ctypes.windll.kernel32.LocalFree(output_blob.pbData)
    return _DPAPI_PREFIX + base64.b64encode(encrypted).decode()


def _dpapi_decrypt(ciphertext_b64: str) -> str:
    if not ciphertext_b64.startswith(_DPAPI_PREFIX):
        return ciphertext_b64   # plaintext fallback (non-Windows dev)

    if sys.platform != "win32":
        raise RuntimeError("DPAPI decrypt called on non-Windows")

    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    data = base64.b64decode(ciphertext_b64[len(_DPAPI_PREFIX):])
    blob_in = ctypes.create_string_buffer(data)
    input_blob  = DATA_BLOB(len(data), blob_in)
    output_blob = DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise RuntimeError("CryptUnprotectData failed")

    plaintext = ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    ctypes.windll.kernel32.LocalFree(output_blob.pbData)
    return plaintext


class ManagerSecretStore:
    def __init__(self, registry: AgentRegistry) -> None:
        self._reg = registry

    def set_secret(self, agent_id: str, key: str, value: str) -> None:
        try:
            encrypted = _dpapi_encrypt(value)
        except Exception as exc:
            logger.warning("DPAPI encrypt failed, storing plaintext: %s", exc)
            encrypted = value
        with self._reg._connect() as conn:
            conn.execute(
                """INSERT INTO agent_secrets (agent_id, key, value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(agent_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (agent_id, key, encrypted, int(time.time() * 1000)),
            )

    def get_secret(self, agent_id: str, key: str) -> str | None:
        with self._reg._connect() as conn:
            row = conn.execute(
                "SELECT value FROM agent_secrets WHERE agent_id=? AND key=?",
                (agent_id, key),
            ).fetchone()
        if not row:
            return None
        try:
            return _dpapi_decrypt(row[0])
        except Exception as exc:
            logger.error("DPAPI decrypt failed for %s/%s: %s", agent_id, key, exc)
            return None

    def delete_agent_secrets(self, agent_id: str) -> None:
        with self._reg._connect() as conn:
            conn.execute("DELETE FROM agent_secrets WHERE agent_id=?", (agent_id,))

    # ── Manager-level convenience helpers ─────────────────────────────────

    def set_manager_secret(self, key: str, value: str) -> None:
        self.set_secret(_MANAGER_SCOPE, key, value)

    def get_manager_secret(self, key: str) -> str | None:
        return self.get_secret(_MANAGER_SCOPE, key)

    def get_activation_key(self) -> str | None:
        return self.get_manager_secret("activation_key")

    def set_activation_key(self, key: str) -> None:
        self.set_manager_secret("activation_key", key)

    def get_api_token(self) -> str | None:
        return self.get_manager_secret("manager_api_token")

    def set_api_token(self, token: str) -> None:
        self.set_manager_secret("manager_api_token", token)

    def get_ipc_token(self) -> str | None:
        return self.get_manager_secret("ipc_token")

    def set_ipc_token(self, token: str) -> None:
        self.set_manager_secret("ipc_token", token)
