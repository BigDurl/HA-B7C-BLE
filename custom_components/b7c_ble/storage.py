"""Private, durable per-bulb mesh state."""

import asyncio
import copy
import json
import os
from pathlib import Path
import stat
import tempfile

from .const import DOMAIN, SEQUENCE_LIMIT


def validate_state(state):
    """Reject malformed state instead of risking sequence or key reuse."""
    if not isinstance(state, dict):
        raise ValueError("invalid B7C mesh state")
    sequence = state.get("next_sequence")
    if type(sequence) is not int or not 1 <= sequence <= SEQUENCE_LIMIT:
        raise ValueError("invalid B7C mesh sequence")
    for field in ("net_key", "app_key"):
        if not isinstance(state.get(field), str) or len(bytes.fromhex(state[field])) != 16:
            raise ValueError(f"invalid B7C {field}")
    if "device_key" in state and (
        not isinstance(state["device_key"], str) or len(bytes.fromhex(state["device_key"])) != 16
    ):
        raise ValueError("invalid B7C device key")
    if state.get("configured") and "device_key" not in state:
        raise ValueError("configured B7C has no device key")


class MeshStore:
    """Write state atomically and propagate failures before any mesh traffic."""

    def __init__(self, hass, address):
        identifier = address.replace(":", "").lower()
        if len(identifier) != 12 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("invalid Bluetooth address")
        self._hass = hass
        self.path = Path(hass.config.path(".storage", f"{DOMAIN}_{identifier}"))

    async def async_load(self):
        return await self._hass.async_add_executor_job(self._load)

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise PermissionError(f"Mesh state {self.path} must be a private regular file")
            state = json.load(file)
            validate_state(state)
            return state

    async def async_save(self, state):
        validate_state(state)
        write = asyncio.ensure_future(
            self._hass.async_add_executor_job(self._save, copy.deepcopy(state))
        )
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    continue
            write.result()
            raise

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".b7c-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(state, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


mesh_store = MeshStore
