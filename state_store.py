import asyncio
import json
import os
import re

LIBRARY_NAME = "jarvis-bridge-state"
STATE_DOC_NAME = "jarvis-state.md"


def _mistral_client():
    api_key = os.environ.get("JARVIS_AGENT_API_KEY", "").strip()
    if api_key:
        from mistralai import Mistral

        return Mistral(api_key=api_key)
    from mistralai.workflows.client import get_mistral_client
    from mistralai.workflows.core.auth import RunAs

    return get_mistral_client(run_as=RunAs.DEPLOYMENT)


def _default_state() -> dict:
    return {"chats": {}, "log": [], "processed": []}


def _find_library(client) -> str | None:
    response = client.libraries.list(search=LIBRARY_NAME, page_size=100)
    for item in response.data or []:
        if item.name == LIBRARY_NAME:
            return item.id
    return None


def _ensure_library(client) -> str:
    library_id = _find_library(client)
    if library_id:
        return library_id
    return client.libraries.create(name=LIBRARY_NAME, description="JARVIS Signal-Bridge State (Cross-Chat-Gedaechtnis)").id


def _parse_state(text: str) -> dict:
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    raw = match.group(1) if match else text
    try:
        state = json.loads(raw)
    except Exception:
        return _default_state()
    if not isinstance(state, dict):
        return _default_state()
    state.setdefault("chats", {})
    state.setdefault("log", [])
    state.setdefault("processed", [])
    return state


def _load_sync() -> dict:
    client = _mistral_client()
    try:
        library_id = _find_library(client)
        if not library_id:
            return _default_state()
        docs = client.documents.list(
            library_id=library_id, search=STATE_DOC_NAME, sort_by="created_at", sort_order="desc", page_size=100
        )
        for doc in docs.data or []:
            if doc.name == STATE_DOC_NAME:
                text = client.documents.text_content(library_id=library_id, document_id=doc.id).text
                return _parse_state(text)
        return _default_state()
    except Exception:
        return _default_state()


def _save_sync(state: dict) -> None:
    client = _mistral_client()
    library_id = _ensure_library(client)
    content = "```json\n" + json.dumps(state, ensure_ascii=False) + "\n```"
    new_doc = client.documents.upload(
        library_id=library_id,
        file={"file_name": STATE_DOC_NAME, "content": content.encode("utf-8"), "content_type": "text/markdown"},
    )
    docs = client.documents.list(library_id=library_id, search=STATE_DOC_NAME, page_size=100)
    for doc in docs.data or []:
        if doc.name == STATE_DOC_NAME and doc.id != new_doc.id:
            try:
                client.documents.delete(library_id=library_id, document_id=doc.id)
            except Exception:
                pass


async def load() -> dict:
    return await asyncio.to_thread(_load_sync)


async def save(state: dict) -> None:
    await asyncio.to_thread(_save_sync, state)


def _upload_sync(file_name: str, content_type: str, data: bytes) -> str:
    client = _mistral_client()
    upload = client.files.upload(file={"file_name": file_name, "content": data}, purpose="ocr")
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
