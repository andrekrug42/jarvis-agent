import os
from datetime import datetime, timezone
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

import mistralai.workflows as workflows

LOG_LIMIT = 200
PROCESSED_LIMIT = 500
LOG_CONTEXT_ENTRIES = 30

_agent_registered = False


async def ensure_agent_registered(agent_name: str, user_api_key: str) -> str:
    """Registriert den Agenten einmalig beim Control Plane (idempotentes Upsert) und gibt den Queue-/Deployment-Namen zurück, an den er gebunden ist.

    Versucht zuerst den User-Key und fällt auf den Service-Account-Token zurück
    (bei gehärtetem Deployment ist der Runtime-SA gepinnt, der User-Key ggf. nicht).
    """
    global _agent_registered
    if _agent_registered:
        return agent_name
    from mistralai.vibe_agents.agents_client.client import AgentsClient

    base_url = os.environ.get("AGENTS_SDK_DEPLOY_CONTROL_PLANE_URL", "https://api.mistral.ai/v2/agents").rstrip("/")
    preferred = os.environ.get("JARVIS_DEPLOYMENT_NAME", "").strip() or agent_name
    keys: list[str] = []
    if user_api_key:
        keys.append(user_api_key)
    token_path = os.environ.get("MISTRAL_SA_TOKEN_PATH", "")
    if token_path and os.path.exists(token_path):
        with open(token_path, encoding="utf-8") as handle:
            sa_token = handle.read().strip()
        if sa_token:
            keys.append(sa_token)
    errors: list[str] = []
    for key in keys:
        try:
            async with AgentsClient(base_url=base_url, api_key=key, timeout_seconds=60) as client:
                response = await client.register_agent(agent_name=agent_name, workflow_name=agent_name, deployment_name=preferred)
            _agent_registered = True
            return response.deployment_name
        except Exception as error:
            errors.append(f"{type(error).__name__}: {str(error)[:200]}")
    raise RuntimeError("register_agent fehlgeschlagen (" + " | ".join(errors) + ")")


class BridgeInput(BaseModel):
    max_messages: int = 25


class BridgeOutput(BaseModel):
    received: int
    processed: int
    details: list[dict[str, Any]]


def _truncate(text: str, limit: int = 240) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def build_envelope(
    message: dict[str, Any],
    files_info: list[dict[str, str]],
    log_lines: list[str],
    audio_transcripts: list[str] | None = None,
    failed_attachments: list[str] | None = None,
    libraries: list[dict] | None = None,
) -> str:
    owner = os.environ.get("JARVIS_OWNER_NAME", "").strip()
    lines = ["[KONTEXT – nur für dich, nicht wörtlich zitieren]"]
    if owner:
        lines.append(f"Account-Inhaber: {owner}")
    lines.append(f"Chat: {message['chat_label']}")
    lines.append(
        f"Absender dieser Nachricht: {message['sender_name']} "
        f"(Nummer: {message['sender_number'] or '–'}, UUID: {message['sender_uuid'] or '–'})"
    )
    if log_lines:
        lines.append("Zuletzt in den anderen Chats besprochen:")
        lines.extend(f"- {entry}" for entry in log_lines)
    if libraries:
        lines.append("Verfügbare Libraries (search_knowledge durchsucht sie; Dokumente werden dort abgelegt):")
        lines.extend(f"- {lib['name']} (id: {lib['id']})" for lib in libraries)
    if audio_transcripts:
        lines.append("Sprachnachrichten (Transkript):")
        lines.extend(f"- {entry}" for entry in audio_transcripts)
    if files_info:
        lines.append("Anhänge (file_id für die Tools describe_image / read_document):")
        for index, info in enumerate(files_info, 1):
            kind = "Bild" if info["content_type"].startswith("image/") else "Dokument"
            lines.append(f"- [{index}] {info['filename']} ({info['content_type']}, {kind}) file_id={info['file_id']}")
    if failed_attachments:
        lines.append("Anhänge, die nicht verarbeitet werden konnten:")
        lines.extend(f"- {entry}" for entry in failed_attachments)
    lines.append(f"[NACHRICHT von {message['sender_name']}]")
    lines.append(message["text"] or "(nur Anhänge, kein Text)")
    return "\n".join(lines)


def extract_file_ids(obj: Any, found: list[str] | None = None) -> list[str]:
    """Sammelt rekursiv alle file_id-Werte aus einem Turn-Record (z. B. generierte Bilder des image_generation-Tools)."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "file_id" and isinstance(value, str) and value not in found:
                found.append(value)
            else:
                extract_file_ids(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            extract_file_ids(value, found)
    else:
        dump = getattr(obj, "model_dump", None)
        if callable(dump):
            try:
                extract_file_ids(dump(), found)
            except Exception:
                pass
    return found


def extract_image_urls(obj: Any, found: list[str] | None = None) -> list[str]:
    """Sammelt rekursiv alle image_url-Werte aus einem Turn-Record (z. B. Ergebnis des Tools generate_image)."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "image_url" and isinstance(value, str) and value.startswith("http") and value not in found:
                found.append(value)
            else:
                extract_image_urls(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            extract_image_urls(value, found)
    else:
        dump = getattr(obj, "model_dump", None)
        if callable(dump):
            try:
                extract_image_urls(dump(), found)
            except Exception:
                pass
    return found


@workflows.activity(start_to_close_timeout=timedelta(seconds=60), retry_policy_max_attempts=2)
async def poll_signal_activity() -> list[dict[str, Any]]:
    import signal_client

    return await signal_client.receive()


@workflows.activity(start_to_close_timeout=timedelta(minutes=20), retry_policy_max_attempts=2)
async def process_message_activity(message: dict[str, Any]) -> dict[str, Any]:
    global _agent_registered

    from mistralai import agents

    import signal_client
    import state_store

    agent_name = os.environ.get("JARVIS_AGENT_NAME", "jarvis")
    user_api_key = os.environ.get("JARVIS_AGENT_API_KEY", "").strip()
    if user_api_key:
        await ensure_agent_registered(agent_name, user_api_key)
        agent = agents.Agent.load_from(agent_name=agent_name, api_key=user_api_key)
    else:
        token_path = os.environ.get("MISTRAL_SA_TOKEN_PATH", "")
        if token_path and os.path.exists(token_path):
            with open(token_path, encoding="utf-8") as handle:
                sa_token = handle.read().strip()
            client = agents.Client(api_key=sa_token) if sa_token else agents.Client()
        else:
            client = agents.Client()
        agent = agents.Agent.load_from(agent_name=agent_name, client=client)

    state = await state_store.load()
    if message["key"] in state.get("processed", []):
        return {"key": message["key"], "status": "already_processed"}

    try:
        await signal_client.send_typing(message["recipients"], stop=False)
    except Exception:
        pass

    files_info: list[dict[str, str]] = []
    audio_transcripts: list[str] = []
    failed_attachments: list[str] = []
    document_files: list[tuple[dict, bytes]] = []
    for attachment in message["attachments"]:
        try:
            data = await signal_client.download_attachment(attachment["id"])
            if attachment["content_type"].startswith("audio/"):
                transcript = await state_store.transcribe_audio(attachment["filename"], data)
                audio_transcripts.append(f"{attachment['filename']}: {transcript or '(kein Text erkannt)'}")
                continue
            file_id = await state_store.upload_attachment(attachment["filename"], attachment["content_type"], data)
            files_info.append(
                {"filename": attachment["filename"], "content_type": attachment["content_type"], "file_id": file_id}
            )
            content_type = attachment["content_type"]
            if not (content_type.startswith(("image/", "video/"))):
                document_files.append((attachment, data))
        except Exception as error:
            failed_attachments.append(f"{attachment['filename']} ({attachment['content_type']}): {str(error)[:160]}")

    try:
        libraries = await state_store.list_family_libraries()
    except Exception:
        libraries = []

    log_lines = [
        f"{entry.get('ts', '')} | {entry.get('chat', '')} | {entry.get('sender', '')}: {entry.get('summary', '')}"
        for entry in state.get("log", [])[-LOG_CONTEXT_ENTRIES:]
    ]
    envelope = build_envelope(
        message,
        files_info,
        log_lines,
        audio_transcripts=audio_transcripts,
        failed_attachments=failed_attachments,
        libraries=libraries,
    )

    chat_state = state.setdefault("chats", {}).setdefault(message["chat_key"], {})
    # Agent-Redeploy erkennen: build_commit_sha des Agenten wird pro Chat gespeichert; bei Änderung
    # neue Session-Epoche (Connectoren werden beim Session-Start aufgelöst — eine alte Session sieht
    # neu hinzugefügte Connectoren nicht).
    try:
        from mistralai.vibe_agents.agents_client.client import AgentsClient

        base_url = os.environ.get("AGENTS_SDK_DEPLOY_CONTROL_PLANE_URL", "https://api.mistral.ai/v2/agents").rstrip("/")
        client_kwargs = {"api_key": user_api_key} if user_api_key else {}
        async with AgentsClient(base_url=base_url, timeout_seconds=30, **client_kwargs) as client:
            detail = await client.get_agent_by_name(agent_name)
        build_sha = getattr(detail, "build_commit_sha", None)
        if build_sha:
            stale = chat_state.get("agent_build") != build_sha and (
                chat_state.get("agent_build") or chat_state.get("session_key")
            )
            if stale:
                chat_state["session_epoch"] = int(chat_state.get("session_epoch", 0)) + 1
                chat_state["session_key"] = f"signal-chat:{message['chat_key']}:e{chat_state['session_epoch']}"
            chat_state["agent_build"] = build_sha
    except Exception:
        pass
    session_key = chat_state.get("session_key") or f"signal-chat:{message['chat_key']}"
    try:
        session = await agent.start_session(idempotency_key=session_key)
    except Exception:
        if user_api_key:
            _agent_registered = False
            await ensure_agent_registered(agent_name, user_api_key)
        chat_state["session_epoch"] = int(chat_state.get("session_epoch", 0)) + 1
        session_key = f"signal-chat:{message['chat_key']}:e{chat_state['session_epoch']}"
        chat_state["session_key"] = session_key
        session = await agent.start_session(idempotency_key=session_key)

    import asyncio

    async def _run_with_retry(sess, turn_key: str):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await sess.run(envelope, idempotency_key=turn_key)
            except Exception as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
        raise last_error

    try:
        record = await _run_with_retry(session, f"signal-msg:{message['key']}")
    except Exception:
        # Session evtl. an einen toten Worker gebunden (503 / OutcomeUnknown nach Redeploy):
        # neue Session ueber Epoch-Schluessel versuchen, damit der Chat wieder antwortet.
        if user_api_key:
            _agent_registered = False
            await ensure_agent_registered(agent_name, user_api_key)
        chat_state["session_epoch"] = int(chat_state.get("session_epoch", 0)) + 1
        session_key = f"signal-chat:{message['chat_key']}:e{chat_state['session_epoch']}"
        chat_state["session_key"] = session_key
        session = await agent.start_session(idempotency_key=session_key)
        record = await _run_with_retry(session, f"signal-msg:{message['key']}")
    if isinstance(record, agents.Busy):
        record = await record.turn.result()

    reply_text = (getattr(record, "text", "") or "").strip()
    generated_file_ids = extract_file_ids(record)

    attachments_out: list[tuple[str, str, bytes]] = []
    for index, file_id in enumerate(generated_file_ids, 1):
        try:
            data = await state_store.download_mistral_file(file_id)
            attachments_out.append((f"jarvis-bild-{index}.png", "image/png", data))
        except Exception:
            pass
    for url in extract_image_urls(record):
        try:
            import httpx

            async with httpx.AsyncClient(timeout=120) as http:
                response = await http.get(url)
                response.raise_for_status()
            mime = response.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
            attachments_out.append((f"jarvis-bild-{len(attachments_out) + 1}.{ext}", mime, response.content))
        except Exception:
            pass

    if reply_text or attachments_out:
        await signal_client.send(message["recipients"], reply_text, attachments_out or None)

    filed_notes: list[str] = []
    if document_files:
        try:
            if not libraries:
                libraries = await state_store.list_family_libraries()
            for attachment, data in document_files:
                try:
                    decision = await state_store.classify_document(
                        attachment["filename"],
                        attachment["content_type"],
                        data,
                        libraries,
                        context=f"{message['sender_name']} im Chat {message['chat_label']}",
                    )
                    library_id = decision.get("library_id")
                    if library_id:
                        await state_store.upload_document_to_library(
                            library_id, attachment["filename"], attachment["content_type"], data
                        )
                        library_name = next((lib["name"] for lib in libraries if lib["id"] == library_id), library_id)
                        filed_notes.append(
                            f"Dokument '{attachment['filename']}' in Library '{library_name}' abgelegt ({decision.get('reason', '')})"
                        )
                except Exception:
                    pass
        except Exception:
            pass

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    summary = _truncate(
        f"{message['text'] or '(Anhang)'} -> {reply_text or '(Bild)'}"
        + (" | " + "; ".join(filed_notes) if filed_notes else "")
    )
    state.setdefault("log", []).append(
        {"ts": now, "chat": message["chat_label"], "sender": message["sender_name"], "summary": summary}
    )
    state["log"] = state["log"][-LOG_LIMIT:]
    state.setdefault("processed", []).append(message["key"])
    state["processed"] = state["processed"][-PROCESSED_LIMIT:]
    try:
        await state_store.save(state)
    except Exception:
        pass

    try:
        await signal_client.send_typing(message["recipients"], stop=True)
        if message.get("timestamp"):
            for target in message.get("receipt_recipients") or message["recipients"]:
                await signal_client.send_read_receipt(target, message["timestamp"])
    except Exception:
        pass

    status = "ok" if isinstance(record, agents.Completed) else type(record).__name__
    return {
        "key": message["key"],
        "status": status,
        "chat": message["chat_key"],
        "reply_chars": len(reply_text),
        "images": len(attachments_out),
        "filed": len(filed_notes),
    }


@workflows.workflow.define(
    name="jarvis-signal-bridge",
    workflow_display_name="JARVIS Signal Bridge",
    workflow_description=(
        "Pollt die Signal-REST-API (v1/receive), beantwortet neue Nachrichten über den "
        "JARVIS-Agenten (eine Session pro Chat) und schickt die Antwort über v2/send zurück."
    ),
)
class SignalBridgeWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: BridgeInput) -> BridgeOutput:
        messages = await poll_signal_activity()
        messages = messages[: input.max_messages]
        details: list[dict[str, Any]] = []
        for message in messages:
            try:
                details.append(await process_message_activity(message))
            except Exception as error:
                details.append({"key": message.get("key"), "status": "error", "error": str(error)[:300]})
        processed = sum(1 for detail in details if detail.get("status") == "ok")
        return BridgeOutput(received=len(messages), processed=processed, details=details)
