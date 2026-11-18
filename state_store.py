import asyncio
import base64
import json
import os
import re

LIBRARY_NAME = "jarvis-bridge-state"
STATE_DOC_NAME = "jarvis-state.md"
FAMILY_LIBRARY_PREFIXES = ("Dokumente zu ",)
FAMILY_LIBRARY_EXTRA_NAMES = {"Allgemeine Dokumente", "Sonstiges"}
CLASSIFY_MAX_CHARS = 20_000
CLASSIFY_MODEL = os.environ.get("JARVIS_CLASSIFY_MODEL", "mistral-small-latest")


def _mistral_client():
    api_key = os.environ.get("JARVIS_AGENT_API_KEY", "").strip()
    if api_key:
        from mistralai.client import Mistral

        return Mistral(api_key=api_key)
    from mistralai.workflows.client import get_mistral_client
    from mistralai.workflows.core.auth import RunAs

    return get_mistral_client(run_as=RunAs.DEPLOYMENT)


def _libraries(client):
    # mistralai 3.x: Libraries liegt unter client.beta.libraries (nicht top-level)
    libraries = getattr(client, "libraries", None)
    if libraries is None:
        libraries = client.beta.libraries
    return libraries


def _documents(client):
    documents = getattr(_libraries(client), "documents", None)
    if documents is not None:
        return documents
    return client.documents


def _default_state() -> dict:
    return {"chats": {}, "log": [], "processed": []}


def _find_library(client) -> str | None:
    response = _libraries(client).list(search=LIBRARY_NAME, page_size=100)
    for item in response.data or []:
        if item.name == LIBRARY_NAME:
            return item.id
    return None


def _ensure_library(client) -> str:
    library_id = _find_library(client)
    if library_id:
        return library_id
    return _libraries(client).create(name=LIBRARY_NAME, description="JARVIS Signal-Bridge State (Cross-Chat-Gedaechtnis)").id


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
        docs = _documents(client).list(
            library_id=library_id, search=STATE_DOC_NAME, sort_by="created_at", sort_order="desc", page_size=100
        )
        for doc in docs.data or []:
            if doc.name == STATE_DOC_NAME:
                text = _documents(client).text_content(library_id=library_id, document_id=doc.id).text
                return _parse_state(text)
        return _default_state()
    except Exception:
        return _default_state()


def _save_sync(state: dict) -> None:
    client = _mistral_client()
    library_id = _ensure_library(client)
    content = "```json\n" + json.dumps(state, ensure_ascii=False) + "\n```"
    new_doc = _documents(client).upload(
        library_id=library_id,
        file={"file_name": STATE_DOC_NAME, "content": content.encode("utf-8"), "content_type": "text/markdown"},
    )
    docs = _documents(client).list(library_id=library_id, search=STATE_DOC_NAME, page_size=100)
    for doc in docs.data or []:
        if doc.name == STATE_DOC_NAME and doc.id != new_doc.id:
            try:
                _documents(client).delete(library_id=library_id, document_id=doc.id)
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


def _transcribe_sync(file_name: str, data: bytes) -> str:
    client = _mistral_client()
    model = os.environ.get("JARVIS_AUDIO_MODEL", "voxtral-mini-latest")
    try:
        result = client.audio.transcriptions.complete(
            model=model, file={"file_name": file_name, "content": data}, language="de"
        )
    except Exception:
        upload = client.files.upload(file={"file_name": file_name, "content": data}, purpose="audio")
        file_id = upload.id if not isinstance(upload, dict) else upload["id"]
        result = client.audio.transcriptions.complete(model=model, file_id=file_id, language="de")
    return (getattr(result, "text", "") or "").strip()


async def transcribe_audio(file_name: str, data: bytes) -> str:
    """Transkribiert eine Sprachnachricht (Audio) per Mistral Voxtral und gibt den Text zurück."""
    return await asyncio.to_thread(_transcribe_sync, file_name, data)


def _download_sync(file_id: str) -> bytes:
    client = _mistral_client()
    response = client.files.download(file_id=file_id)
    return response.read() if hasattr(response, "read") else bytes(response)


async def download_mistral_file(file_id: str) -> bytes:
    """Lädt eine Datei (z. B. ein generiertes Bild) aus der Mistral-Files-API herunter."""
    return await asyncio.to_thread(_download_sync, file_id)


def _family_libraries_sync() -> list[dict]:
    client = _mistral_client()
    response = _libraries(client).list(page_size=100)
    libraries = []
    for item in getattr(response, "data", None) or []:
        name = getattr(item, "name", "") or ""
        if name.startswith(FAMILY_LIBRARY_PREFIXES) or name in FAMILY_LIBRARY_EXTRA_NAMES:
            libraries.append({"id": item.id, "name": name})
    return libraries


async def list_family_libraries() -> list[dict]:
    """Listet die Familien-Libraries (Dokumente zu <Name>, Allgemeine Dokumente, Sonstiges)."""
    return await asyncio.to_thread(_family_libraries_sync)


def _document_text_sync(client, filename: str, content_type: str, data: bytes) -> str:
    if content_type == "application/pdf" or data.startswith(b"%PDF"):
        try:
            response = client.ocr.process(
                model="mistral-ocr-latest",
                document={
                    "type": "document_url",
                    "document_url": f"data:application/pdf;base64,{base64.b64encode(data).decode()}",
                },
            )
            return "\n".join(getattr(page, "markdown", "") or "" for page in response.pages)[:CLASSIFY_MAX_CHARS]
        except Exception:
            return ""
    if content_type.startswith("text/"):
        return data[:CLASSIFY_MAX_CHARS].decode("utf-8", "ignore")
    return ""


def _classify_sync(filename: str, content_type: str, data: bytes, libraries: list[dict], context: str) -> dict:
    client = _mistral_client()
    text = _document_text_sync(client, filename, content_type, data)
    lib_desc = "\n".join(f"- {lib['name']} (id: {lib['id']})" for lib in libraries)
    prompt = (
        "Du bekommst ein per Signal empfangenes Dokument und eine Liste von Libraries. "
        "Entscheide, ob das Dokument langfristig aufbewahrt werden soll und in welche Library es gehört. "
        "Regeln: Dokumente, die eine konkrete Person betreffen (Name im Inhalt/Absender), kommen in deren Library "
        "'Dokumente zu <Name>'; allgemeine Dokumente (Verträge, Anleitungen, Rechnungen der Familie) in "
        "'Allgemeine Dokumente'; alles andere Unwichtige (z. B. flüchtige Screenshots ohne Bezug) nach 'Sonstiges' "
        "ODER null, wenn es gar nicht abgelegt werden soll (reiner Chat-Content ohne Mehrwert). "
        "Antworte AUSSCHLIESSLICH mit einem JSON-Objekt: {\"library_id\": \"<id oder null>\", \"reason\": \"<kurzer Grund auf Deutsch>\"}.\n\n"
        f"Libraries:\n{lib_desc}\n\nKontext: {context}\nDatei: {filename} ({content_type})\n"
        f"Auszug des Inhalts:\n{text or '(kein Text extrahierbar, nur Dateiname/Typ)'}"
    )
    response = client.chat.complete(
        model=CLASSIFY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    content = response.choices[0].message.content
    raw = content if isinstance(content, str) else str(content)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        decision = json.loads(match.group(0)) if match else {}
    except Exception:
        decision = {}
    library_id = decision.get("library_id")
    valid_ids = {lib["id"] for lib in libraries}
    if library_id not in valid_ids:
        library_id = None
    reason = decision.get("reason") if isinstance(decision.get("reason"), str) else ""
    return {"library_id": library_id, "reason": reason[:200]}


async def classify_document(
    filename: str, content_type: str, data: bytes, libraries: list[dict], context: str
) -> dict:
    """Entscheidet per LLM, ob ein Dokument in eine der Libraries abgelegt werden soll (und welche)."""
    return await asyncio.to_thread(_classify_sync, filename, content_type, data, libraries, context)


def _upload_document_sync(library_id: str, filename: str, content_type: str, data: bytes) -> str:
    client = _mistral_client()
    doc = _documents(client).upload(
        library_id=library_id, file={"file_name": filename, "content": data, "content_type": content_type}
    )
    return doc.id if not isinstance(doc, dict) else doc["id"]


async def upload_document_to_library(library_id: str, filename: str, content_type: str, data: bytes) -> str:
    """Lädt ein Dokument in eine Library hoch (mit User-Key, da der Deployment-SA dort nur lesen darf)."""
    return await asyncio.to_thread(_upload_document_sync, library_id, filename, content_type, data)
