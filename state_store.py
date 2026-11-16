import asyncio
import json

STATE_FILE_NAME = "jarvis-bridge-state.json"


def _mistral_client():
    from mistralai.workflows.client import get_mistral_client
    from mistralai.workflows.core.auth import RunAs

    return get_mistral_client(run_as=RunAs.DEPLOYMENT)


def _default_state() -> dict:
    return {"chats": {}, "log": [], "processed": []}


def _load_sync() -> dict:
    client = _mistral_client()
    try:
        listing = client.files.list()
        files = getattr(listing, "data", listing) or []
        candidates = [
            f for f in files
            if getattr(f, "file_name", None) == STATE_FILE_NAME or (isinstance(f, dict) and f.get("file_name") == STATE_FILE_NAME)
        ]
        if not candidates:
            return _default_state()
        def _created(f):
            value = getattr(f, "created_at", None) if not isinstance(f, dict) else f.get("created_at")
            return value or ""
        newest = max(candidates, key=_created)
        file_id = newest.id if not isinstance(newest, dict) else newest["id"]
        response = client.files.download(file_id=file_id)
        raw = response.read() if hasattr(response, "read") else bytes(response)
        state = json.loads(raw.decode())
        if not isinstance(state, dict):
            return _default_state()
        state.setdefault("chats", {})
        state.setdefault("log", [])
        state.setdefault("processed", [])
        return state
    except Exception:
        return _default_state()


def _save_sync(state: dict) -> None:
    client = _mistral_client()
    listing = client.files.list()
    files = getattr(listing, "data", listing) or []
    old_ids = [
        (f.id if not isinstance(f, dict) else f.get("id"))
        for f in files
        if (getattr(f, "file_name", None) if not isinstance(f, dict) else f.get("file_name")) == STATE_FILE_NAME
    ]
    upload = client.files.upload(
        file={"file_name": STATE_FILE_NAME, "content": json.dumps(state).encode()},
        purpose="batch",
    )
    new_id = upload.id if not isinstance(upload, dict) else upload.get("id")
    for old_id in old_ids:
        if old_id and old_id != new_id:
            try:
                client.files.delete(file_id=old_id)
            except Exception:
                pass


async def load() -> dict:
    return await asyncio.to_thread(_load_sync)


async def save(state: dict) -> None:
    await asyncio.to_thread(_save_sync, state)


def _upload_sync(file_name: str, content_type: str, data: bytes) -> str:
    client = _mistral_client()
    purpose = "vision" if content_type.startswith("image/") else "ocr"
    upload = client.files.upload(file={"file_name": file_name, "content": data}, purpose=purpose)
    return upload.id if not isinstance(upload, dict) else upload["id"]


async def upload_attachment(file_name: str, content_type: str, data: bytes) -> str:
    """Lädt einen Signal-Anhang in die Mistral-Files-API hoch und gibt die file_id zurück."""
    return await asyncio.to_thread(_upload_sync, file_name, content_type, data)


def _download_sync(file_id: str) -> bytes:
    client = _mistral_client()
    response = client.files.download(file_id=file_id)
    return response.read() if hasattr(response, "read") else bytes(response)


async def download_mistral_file(file_id: str) -> bytes:
    """Lädt eine Datei (z. B. ein generiertes Bild) aus der Mistral-Files-API herunter."""
    return await asyncio.to_thread(_download_sync, file_id)
