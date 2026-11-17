import asyncio
import base64
import json
import os

from mistralai import agents

MAX_DOCUMENT_CHARS = 120_000

SEARCH_MODEL = os.environ.get("JARVIS_SEARCH_MODEL", "mistral-large-4")
LIBRARY_NAME_PREFIXES = ("Dokumente zu ",)
LIBRARY_EXTRA_NAMES = {"Allgemeine Dokumente", "Sonstiges"}
# Feste IDs der Familien-Libraries (ueber die Workspace-Freigabe als Viewer nutzbar).
# Der SA-Token der Deployment darf die Libraries-List-API nicht aufrufen (401 "User id is required"),
# deshalb wird die Liste nicht per API aufgelöst.
DEFAULT_LIBRARY_IDS = [
    "019ffb25-68fa-7444-b829-ff7d4a456b9a",  # Dokumente zu Andre
    "019ffb25-44c9-715d-a5c6-ef6929e9ec55",  # Dokumente zu Anna
    "019ffb25-0f55-736c-b9c3-6d1b637ec09d",  # Dokumente zu Melina
    "019ffb24-b3a0-77be-aa31-cfd6f554ccc6",  # Dokumente zu Magda
    "019fd1b4-919e-73bf-8d53-e619b3762d22",  # Dokumente zu Jano
    "019ffb25-9d04-77af-bad2-02413a95b442",  # Allgemeine Dokumente
    "019ffb25-c58a-7324-978c-48ea8cfa5d0a",  # Sonstiges
]
_library_ids_cache: list[str] | None = None


def _mistral_client():
    # User-Key bevorzugen, wenn gebunden: der SA-Token der Deployment hat keine User-ID
    # und Libraries-/Session-APIs verweigern ihm den Zugriff (401 "User id is required").
    api_key = os.environ.get("JARVIS_AGENT_API_KEY")
    if api_key:
        from mistralai.client import Mistral

        return Mistral(api_key=api_key)
    from mistralai.workflows.client import get_mistral_client
    from mistralai.workflows.core.auth import RunAs

    return get_mistral_client(run_as=RunAs.DEPLOYMENT)


def _as_tool_error(fn):
    # Fehler als agents.ToolError weiterreichen, damit das Modell die echte Meldung sieht
    # (sonst nur "unexpected error") und wir sie ueber den State-Log diagnostizieren koennen.
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except agents.ToolError:
            raise
        except Exception as exc:
            raise agents.ToolError(f"{type(exc).__name__}: {str(exc)[:800]}") from exc

    return wrapper


def _download_file(client, file_id: str) -> bytes:
    response = client.files.download(file_id=file_id)
    if hasattr(response, "read"):
        return response.read()
    return bytes(response)


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _ocr_document(data: bytes) -> tuple[str, str]:
    if data.startswith(b"%PDF"):
        return "document_url", f"data:application/pdf;base64,{base64.b64encode(data).decode()}"
    if data.startswith((b"\x89PNG", b"\xff\xd8", b"GIF8")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        return "image_url", f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode()}"
    return "document_url", f"data:application/octet-stream;base64,{base64.b64encode(data).decode()}"


@agents.tool
async def describe_image(file_id: str, question: str = "Beschreibe das Bild ausführlich: Was ist zu sehen, welche Texte/Elemente gibt es?") -> str:
    """Versteht ein Bild (Foto, Screenshot, Grafik, Meme): liest Texte auf dem Bild und beschreibt den Inhalt.

    `file_id`: die file_id des Bild-Anhangs aus dem KONTEXT-Block der Nachricht.
    `question`: die konkrete Frage zum Bild, z. B. "Was steht auf dem Whiteboard?".
    """
    def _run() -> str:
        client = _mistral_client()
        data = _download_file(client, file_id)
        data_url = f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode()}"
        response = client.chat.complete(
            model="mistral-large-4",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else str(content)

    return await asyncio.to_thread(_as_tool_error(_run))


@agents.tool
async def read_document(file_id: str, question: str = "") -> str:
    """Liest ein PDF oder Dokument per OCR und gibt den Inhalt als Markdown zurück.

    `file_id`: die file_id des Dokument-Anhangs aus dem KONTEXT-Block der Nachricht.
    `question`: optional eine konkrete Frage zum Dokument; dann wird das Dokument gelesen und die Frage direkt beantwortet.
    """
    def _download() -> bytes:
        return _download_file(_mistral_client(), file_id)

    data = await asyncio.to_thread(_as_tool_error(_download))

    def _ocr() -> str:
        client = _mistral_client()
        doc_type, data_url = _ocr_document(data)
        response = client.ocr.process(
            model="mistral-ocr-latest",
            document={"type": doc_type, doc_type: data_url},
        )
        pages = [getattr(page, "markdown", "") or "" for page in response.pages]
        text = "\n\n".join(pages).strip()
        if len(text) > MAX_DOCUMENT_CHARS:
            text = text[:MAX_DOCUMENT_CHARS] + "\n\n[... Dokument gekürzt ...]"
        return text

    text = await asyncio.to_thread(_as_tool_error(_ocr))

    if question:
        def _qa() -> str:
            client = _mistral_client()
            response = client.chat.complete(
                model="mistral-large-4",
                messages=[
                    {
                        "role": "system",
                        "content": "Beantworte die Frage ausschließlich anhand des folgenden Dokuments. Antworte auf Deutsch, prägnant.",
                    },
                    {"role": "user", "content": f"Dokument:\n\n{text}\n\nFrage: {question}"},
                ],
            )
            content = response.choices[0].message.content
            return content if isinstance(content, str) else str(content)

        return await asyncio.to_thread(_as_tool_error(_qa))

    return text or "(Das Dokument enthielt keinen erkennbaren Text.)"


def _conversations(client):
    conversations = getattr(client, "conversations", None)
    if conversations is None:
        conversations = client.beta.conversations
    return conversations


def _conversation_sync(model: str, tools: list[dict], inputs: str):
    client = _mistral_client()
    return _conversations(client).start(model=model, tools=tools, inputs=inputs)


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def _collect_text_urls(obj, texts: list[str], urls: list[str]) -> None:
    if isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            texts.append(obj["text"])
        annotations = obj.get("annotations")
        if isinstance(annotations, list):
            for ann in annotations:
                if isinstance(ann, dict):
                    citation = ann.get("url_citation")
                    if isinstance(citation, dict) and citation.get("url"):
                        urls.append(citation["url"])
        for value in obj.values():
            _collect_text_urls(value, texts, urls)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _collect_text_urls(item, texts, urls)


def _conversation_text(response) -> tuple[str, list[str]]:
    texts: list[str] = []
    urls: list[str] = []
    for output in getattr(response, "outputs", None) or []:
        dump = _dump(output)
        if isinstance(dump, dict) and dump.get("type") == "message.output":
            content = dump.get("content")
            if isinstance(content, str):
                texts.append(content)
            else:
                _collect_text_urls(content, texts, urls)
    return "\n".join(part for part in texts if part).strip(), list(dict.fromkeys(urls))


def _find_first_url(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "url" and isinstance(value, str) and value.startswith("http"):
                return value
            found = _find_first_url(value)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_first_url(item)
            if found:
                return found
    return None


@agents.tool
async def web_search(query: str) -> str:
    """Sucht im Web nach aktuellen Informationen (Wetter, Nachrichten, Preise, Öffnungszeiten, Sportergebnisse ...).
    Gibt eine prägnante Antwort auf Deutsch zurück, inklusive der gefundenen Quellen-URLs.

    `query`: die Suchfrage, z. B. "Wettervorhersage Hamburg morgen".
    """
    def _run():
        return _conversation_sync(SEARCH_MODEL, [{"type": "web_search"}], query)

    response = await asyncio.to_thread(_as_tool_error(_run))
    text, urls = _conversation_text(response)
    if not text:
        return "(Die Websuche lieferte keine Antwort.)"
    if urls:
        return f"{text}\n\nQuellen: {', '.join(urls[:8])}"
    return text


@agents.tool
async def generate_image(prompt: str) -> dict:
    """Generiert ein Bild (Foto, Grafik, Illustration) anhand einer Beschreibung.
    Gibt {"image_url": <URL des fertigen Bildes>} zurück; das Bild wird automatisch als Anhang an den Chat geschickt.

    `prompt`: genaue Bildbeschreibung, z. B. "R futuristisches Smart-Home-Dashboard, dunkles Design".
    """
    def _run():
        return _conversation_sync(SEARCH_MODEL, [{"type": "image_generation"}], prompt)

    response = await asyncio.to_thread(_as_tool_error(_run))
    for output in getattr(response, "outputs", None) or []:
        dump = _dump(output)
        if isinstance(dump, dict) and dump.get("type") == "tool.execution" and "image" in str(dump.get("name", "")).lower():
            info = dump.get("info") or {}
            result = info.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except Exception:
                    result = None
            url = None
            if isinstance(result, dict):
                url = result.get("url")
            if not url:
                url = _find_first_url(info)
            if url:
                return {"image_url": url}
    return {"image_url": "", "error": "Die Bildgenerierung lieferte kein Ergebnis."}


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


FAMILY_LIBRARY_PREFIX = "Dokumente zu "
CONTACT_DOC_PREFIX = "Kontaktdaten "


def _find_library_for_person(client, person: str):
    response = _libraries(client).list(page_size=100)
    libraries = list(getattr(response, "data", None) or [])
    target = f"{FAMILY_LIBRARY_PREFIX}{person}".strip().lower()
    for item in libraries:
        if (getattr(item, "name", "") or "").lower() == target:
            return item.id, item.name
    for fallback in ("Allgemeine Dokumente", "Sonstiges"):
        for item in libraries:
            if (getattr(item, "name", "") or "") == fallback:
                return item.id, item.name
    return None


def _contact_doc_name(person: str) -> str:
    return f"{CONTACT_DOC_PREFIX}{person}.md"


def _save_person_info_sync(person: str, info: str) -> str:
    client = _mistral_client()
    found = _find_library_for_person(client, person)
    if not found:
        raise agents.ToolError(f"Keine Library für '{person}' gefunden (erwartet '{FAMILY_LIBRARY_PREFIX}{person}').")
    library_id, library_name = found
    doc_name = _contact_doc_name(person)
    content = f"# Kontaktdaten {person}\n\n{info.strip()}\n"
    new_doc = _documents(client).upload(
        library_id=library_id,
        file={"file_name": doc_name, "content": content.encode("utf-8"), "content_type": "text/markdown"},
    )
    new_id = new_doc.id if not isinstance(new_doc, dict) else new_doc["id"]
    docs = _documents(client).list(library_id=library_id, search=doc_name, page_size=100)
    for doc in getattr(docs, "data", None) or []:
        if doc.name == doc_name and doc.id != new_id:
            try:
                _documents(client).delete(library_id=library_id, document_id=doc.id)
            except Exception:
                pass
    return f"Kontaktdaten von {person} gespeichert in Library '{library_name}' (Dokument '{doc_name}')."


def _read_person_info_sync(person: str) -> str:
    client = _mistral_client()
    found = _find_library_for_person(client, person)
    if not found:
        return f"(Keine Library für '{person}' gefunden.)"
    library_id, library_name = found
    doc_name = _contact_doc_name(person)
    docs = _documents(client).list(library_id=library_id, search=doc_name, page_size=100)
    for doc in getattr(docs, "data", None) or []:
        if doc.name == doc_name:
            text = _documents(client).text_content(library_id=library_id, document_id=doc.id).text
            return f"[Kontaktdaten {person} – Library '{library_name}']\n{text}"
    return f"(Noch keine Kontaktdaten für '{person}' gespeichert.)"


def _resolve_library_ids() -> list[str]:
    global _library_ids_cache
    if _library_ids_cache is not None:
        return _library_ids_cache
    env_ids = os.environ.get("JARVIS_LIBRARY_IDS", "").strip()
    if env_ids:
        _library_ids_cache = [part.strip() for part in env_ids.split(",") if part.strip()]
        return _library_ids_cache
    _library_ids_cache = list(DEFAULT_LIBRARY_IDS)
    if not _library_ids_cache:
        client = _mistral_client()
        response = _libraries(client).list(page_size=100)
        ids = []
        for item in getattr(response, "data", None) or []:
            name = getattr(item, "name", "") or ""
            if name.startswith(LIBRARY_NAME_PREFIXES) or name in LIBRARY_EXTRA_NAMES:
                ids.append(item.id)
        _library_ids_cache = ids
    return _library_ids_cache


@agents.tool
async def search_knowledge(query: str) -> str:
    """Sucht in den Familien-Libraries (Dokumente zu Andre/Anna/Melina/Magda/Jano, Allgemeine Dokumente, Sonstiges)
    und beantwortet die Frage anhand der gefundenen Dokumente; nennt die Dokumentnamen als Quellen.

    `query`: die Frage, z. B. "Was hat Anna über den Urlaub im August geschrieben?".
    """
    def _run() -> str:
        library_ids = _resolve_library_ids()
        if not library_ids:
            return "(Aktuell sind keine Libraries verfügbar.)"
        inputs = (
            "Beantworte die folgende Frage ausschließlich anhand der Dokumente in den verbundenen Libraries. "
            "Antworte auf Deutsch, prägnant, und nenne am Ende die Dokumentnamen als Quellen.\n\nFrage: " + query
        )
        response = _conversation_sync(
            SEARCH_MODEL, [{"type": "document_library", "library_ids": library_ids}], inputs
        )
        text, _ = _conversation_text(response)
        return text or "(In den Libraries wurde nichts Passendes gefunden.)"

    return await asyncio.to_thread(_as_tool_error(_run))


@agents.tool
async def save_person_info(person: str, info: str) -> str:
    """Speichert persönliche Daten einer Person dauerhaft (E-Mail, Adresse, Telefon, Geburtstag ...)
    in deren Library ('Dokumente zu <Vorname>' als Dokument 'Kontaktdaten <Vorname>.md'), damit sie
    später bei Mails, Einladungen und Antworten verwendet werden.

    `person`: Vorname der Person, z. B. "Anna".
    `info`: die Kontaktdaten als Text/Markdown, z. B. "E-Mail: anna@beispiel.de, Adresse: Musterstr. 1, 12345 Stadt".
    """
    def _run() -> str:
        return _save_person_info_sync(person.strip(), info)

    return await asyncio.to_thread(_as_tool_error(_run))


@agents.tool
async def read_person_info(person: str) -> str:
    """Liest die gespeicherten Kontaktdaten einer Person (E-Mail, Adresse, Telefon ...) aus deren Library.
    Vor dem Entwerfen von Mails/Einladungen immer zuerst aufrufen, um die richtige Adresse zu verwenden.

    `person`: Vorname der Person, z. B. "Anna".
    """
    def _run() -> str:
        return _read_person_info_sync(person.strip())

    return await asyncio.to_thread(_as_tool_error(_run))


@agents.tool
async def save_mail_attachment(filename: str, content_base64: str, mime_type: str = "application/octet-stream") -> str:
    """Speichert einen base64-kodierten Mail-Anhang aus dem gmail-Connector in der Mistral-Files-API
    und gibt die file_id zurück — damit kann der Anhang danach mit read_document (PDF/Dokumente)
    oder describe_image (Bilder) gelesen werden.

    `filename`: Dateiname des Anhangs, z. B. "rechnung.pdf".
    `content_base64`: base64-kodierter Inhalt (aus get_message_attachment(messageId, attachmentId) bzw. get_message mit FULL_CONTENT).
    `mime_type`: MIME-Typ des Anhangs, z. B. "application/pdf".
    """
    def _run() -> str:
        client = _mistral_client()
        try:
            data = base64.b64decode(content_base64, validate=False)
        except Exception as exc:
            raise agents.ToolError(f"base64-Dekodierung fehlgeschlagen: {exc}")
        upload = client.files.upload(file={"file_name": filename, "content": data}, purpose="ocr")
        file_id = upload.id if not isinstance(upload, dict) else upload["id"]
        return f"Anhang '{filename}' in der Files-API gespeichert: file_id={file_id} ({len(data)} Bytes, {mime_type})."

    return await asyncio.to_thread(_as_tool_error(_run))


@agents.tool
async def file_attachment_to_library(file_id: str, filename: str, person: str = "") -> str:
    """Lädt eine Datei aus der Mistral-Files-API (file_id, z. B. von save_mail_attachment) in die passende
    Familien-Library hoch: bei `person` in 'Dokumente zu <person>', sonst per LLM-Einschätzung
    ('Allgemeine Dokumente' bzw. 'Sonstiges'; wenn nichts passt, wird nicht abgelegt).

    `file_id`: file_id aus der Files-API.
    `filename`: Dateiname, unter dem das Dokument in der Library abgelegt wird.
    `person`: optional Vorname der Person, um die es geht, z. B. "Anna".
    """
    import mimetypes

    import state_store

    def _download() -> bytes:
        return _download_file(_mistral_client(), file_id)

    data = await asyncio.to_thread(_as_tool_error(_download))
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    person = (person or "").strip()
    if person:
        def _find():
            return _find_library_for_person(_mistral_client(), person)

        found = await asyncio.to_thread(_as_tool_error(_find))
        if not found:
            raise agents.ToolError(f"Keine Library für '{person}' gefunden (erwartet '{FAMILY_LIBRARY_PREFIX}{person}').")
        library_id, library_name = found
    else:
        libraries = await state_store.list_family_libraries()
        decision = await state_store.classify_document(
            filename, mime, data, libraries, context="Mail-Anhang aus dem JARVIS-Postfach"
        )
        library_id = decision.get("library_id")
        library_name = next((lib["name"] for lib in libraries if lib["id"] == library_id), None)
        if not library_id:
            return f"Anhang '{filename}' wurde NICHT abgelegt (Einschätzung: {decision.get('reason') or 'kein Mehrwert'})."
    doc_id = await state_store.upload_document_to_library(library_id, filename, mime, data)
    return f"Anhang '{filename}' in Library '{library_name}' abgelegt (Dokument-ID {doc_id})."


@agents.tool
async def send_signal_message(target: str, message: str) -> str:
    """Sendet proaktiv eine Signal-Nachricht an ein Familienmitglied oder die Gruppe
    (z. B. wenn jemand sagt: „sag Anna, dass das Treffen um 18 Uhr ist“ oder „schreib der Gruppe …“).

    `target`: Vorname/Kontaktname (z. B. "Anna"), '+Nummer', UUID, oder 'gruppe' / Gruppenname (z. B. "Die Kruhsohs").
    `message`: Nachrichtentext auf Deutsch.
    Sendet NUR an Chats aus der Allowlist (JARVIS_ALLOWED_CHATS); bei nicht erlaubtem Ziel wird ein Fehler
    zurückgegeben und nichts gesendet. Benötigt die SIGNAL_*-Umgebungsvariablen in der Deployment.
    """
    import signal_client
    import state_store

    try:
        recipients, chat_key, label = await signal_client.resolve_target(target)
    except Exception as exc:
        msg = str(exc)
        if "SIGNAL_API_URL" in msg or "SIGNAL_PHONE_NUMBER" in msg:
            raise agents.ToolError(
                "Signal-API nicht konfiguriert: An der Agent-Deployment 'jarvis' fehlen die Secrets "
                "signal_api_url/signal_phone_number/signal_username/signal_password (-> SIGNAL_*) und "
                "jarvis_allowed_chats (-> JARVIS_ALLOWED_CHATS). Originalfehler: " + msg
            )
        raise agents.ToolError(f"Ziel nicht auflösbar/erlaubt: {msg}")
    await signal_client.send(recipients, message)
    try:
        from datetime import datetime, timezone

        state = await state_store.load()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        state.setdefault("log", []).append(
            {"ts": now, "chat": label, "sender": "JARVIS (proaktiv)", "summary": message[:240]}
        )
        state["log"] = state["log"][-200:]
        await state_store.save(state)
    except Exception:
        pass
    return f"Erledigt ✅ Nachricht an {label} gesendet."


@agents.tool
async def schedule_task(when_iso: str, action: str, target: str = "") -> str:
    """Legt eine zeitgesteuerte Aufgabe an. Die Bridge führt sie zum Zeitpunkt automatisch aus
    (z. B. Erinnerung, Posteingang prüfen, E-Mail schreiben) und schickt das Ergebnis per Signal
    in den Ziel-Chat.

    `when_iso`: Zeitpunkt als ISO-8601 mit Zeitzone, z. B. "2026-11-20T18:00:00+01:00" (Europe/Berlin).
    `action`: klarer, vollständiger Auftrag auf Deutsch, z. B. "Erinnere daran, die Wäsche aufzuhängen".
    `target`: Chat-Key aus dem KONTEXT-Block (Standardziel: derselbe Chat) oder Name/Gruppe des Empfängers.
    """
    import state_store
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    try:
        due = datetime.fromisoformat(when_iso.strip())
    except Exception:
        raise agents.ToolError(f"when_iso ist kein gültiges ISO-8601-Datum: {when_iso!r}")
    if due.tzinfo is None:
        due = due.replace(tzinfo=ZoneInfo("Europe/Berlin"))
    now = datetime.now(timezone.utc)
    due_ts = due.timestamp()
    if due_ts < now.timestamp() + 30:
        raise agents.ToolError("Zeitpunkt liegt in der Vergangenheit (mindestens +30 Sekunden ab jetzt)")
    task = {
        "id": f"task-{int(now.timestamp() * 1000)}",
        "due": due_ts,
        "due_iso": due.isoformat(),
        "action": action.strip()[:2000],
        "target": target.strip(),
        "status": "pending",
        "created": now.timestamp(),
    }
    state = await state_store.load()
    tasks = state.setdefault("tasks", [])
    open_count = sum(1 for t in tasks if t.get("status") in ("pending", "running"))
    if open_count >= 100:
        raise agents.ToolError("zu viele offene Aufgaben (max. 100) – alte erledigen lassen oder aufräumen")
    tasks.append(task)
    state["tasks"] = [
        t for t in tasks if t.get("status") in ("pending", "running") or now.timestamp() - float(t.get("finished", now.timestamp())) < 7 * 86400
    ][-200:]
    await state_store.save(state)
    return (
        f"Aufgabe angelegt ✅ {due.strftime('%d.%m.%Y um %H:%M')} ({due.tzinfo}) – „{action.strip()[:120]}“"
        f" (Ziel: {target.strip() or 'aktueller Chat'})"
    )
