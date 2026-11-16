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
    """Registriert den Agenten einmalig beim Control Plane (idempotentes Upsert) und gibt den Queue-/Deployment-Namen zurück, an den er gebunden ist."""
    global _agent_registered
    if _agent_registered:
        return agent_name
    from mistralai.vibe_agents.agents_client.client import AgentsClient

    base_url = os.environ.get("AGENTS_SDK_DEPLOY_CONTROL_PLANE_URL", "https://api.mistral.ai/v2/agents").rstrip("/")
    preferred = os.environ.get("JARVIS_DEPLOYMENT_NAME", "").strip() or agent_name
    async with AgentsClient(base_url=base_url, api_key=user_api_key, timeout_seconds=60) as client:
        response = await client.register_agent(agent_name=agent_name, workflow_name=agent_name, deployment_name=preferred)
    _agent_registered = True
    return response.deployment_name


class BridgeInput(BaseModel):
    max_messages: int = 25


class BridgeOutput(BaseModel):
    received: int
    processed: int
    details: list[dict[str, Any]]


def _truncate(text: str, limit: int = 240) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def build_envelope(message: dict[str, Any], files_info: list[dict[str, str]], log_lines: list[str]) -> str:
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
    if files_info:
        lines.append("Anhänge (file_id für die Tools describe_image / read_document):")
        for index, info in enumerate(files_info, 1):
            kind = "Bild" if info["content_type"].startswith("image/") else "Dokument"
            lines.append(f"- [{index}] {info['filename']} ({info['content_type']}, {kind}) file_id={info['file_id']}")
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

    files_info: list[dict[str, str]] = []
    for attachment in message["attachments"]:
        data = await signal_client.download_attachment(attachment["id"])
        file_id = await state_store.upload_attachment(attachment["filename"], attachment["content_type"], data)
        files_info.append(
            {"filename": attachment["filename"], "content_type": attachment["content_type"], "file_id": file_id}
        )

    log_lines = [
        f"{entry.get('ts', '')} | {entry.get('chat', '')} | {entry.get('sender', '')}: {entry.get('summary', '')}"
        for entry in state.get("log", [])[-LOG_CONTEXT_ENTRIES:]
    ]
    envelope = build_envelope(message, files_info, log_lines)

    chat_state = state.setdefault("chats", {}).setdefault(message["chat_key"], {})
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

    record = await session.run(envelope, idempotency_key=f"signal-msg:{message['key']}")
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

    if reply_text or attachments_out:
        await signal_client.send(message["recipients"], reply_text, attachments_out or None)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    summary = _truncate(f"{message['text'] or '(Anhang)'} -> {reply_text or '(Bild)'}")
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

    status = "ok" if isinstance(record, agents.Completed) else type(record).__name__
    return {"key": message["key"], "status": status, "chat": message["chat_key"], "reply_chars": len(reply_text), "images": len(attachments_out)}


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
