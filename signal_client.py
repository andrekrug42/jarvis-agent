import base64
import os

import httpx

RECEIVE_TIMEOUT_SECONDS = 30


def _b64decode(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode()
    except Exception:
        return value


def _basic_token(username: str, password: str) -> str:
    # Die Secrets liegen base64-kodiert vor (base64(user), base64(pass)); der Standard-Header ist base64(user:pass).
    user = _b64decode(username)
    password = _b64decode(password)
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def _config() -> tuple[str, str, dict[str, str]]:
    base = os.environ.get("SIGNAL_API_URL", "").rstrip("/")
    number = os.environ.get("SIGNAL_PHONE_NUMBER", "")
    if not base or not number:
        raise RuntimeError("SIGNAL_API_URL und SIGNAL_PHONE_NUMBER müssen als Umgebungsvariablen gesetzt sein")
    headers = {}
    username = os.environ.get("SIGNAL_USERNAME", "")
    password = os.environ.get("SIGNAL_PASSWORD", "")
    if username and password:
        headers["Authorization"] = f"Basic {_basic_token(username, password)}"
    else:
        key = os.environ.get("SIGNAL_API_KEY", "")
        if key:
            header = os.environ.get("SIGNAL_AUTH_HEADER", "Authorization")
            prefix = os.environ.get("SIGNAL_AUTH_PREFIX", "Bearer ")
            headers[header] = f"{prefix}{key}"
    return base, number, headers


def _allowed_sources() -> set[str] | None:
    raw = os.environ.get("SIGNAL_ALLOWED_SOURCES", "").strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _is_own_message(env: dict, own_number: str) -> bool:
    source_number = env.get("sourceNumber") or env.get("source") or ""
    return bool(source_number) and source_number == own_number


def normalize(entry: dict) -> dict | None:
    """Formt einen v2/receive-Envelope in ein flaches Nachrichten-Dict um. None = ignorieren (z. B. Receipts, eigene Nachrichten)."""
    env = entry.get("envelope") or {}
    dm = env.get("dataMessage")
    if not dm:
        return None
    _, own_number, _ = _config()
    if _is_own_message(env, own_number):
        return None
    source_uuid = env.get("sourceUuid") or env.get("source") or ""
    source_number = env.get("sourceNumber") or env.get("source") or ""
    source_name = env.get("sourceName") or ""
    allowed = _allowed_sources()
    if allowed is not None and source_uuid not in allowed and source_number not in allowed:
        return None
    group_info = dm.get("groupInfo") or {}
    group_id = group_info.get("groupId")
    group_name = group_info.get("name") or ""
    if group_id:
        chat_key = f"group:{group_id}"
        chat_label = f'Gruppenchat "{group_name}"' if group_name else f"Gruppe {group_id}"
        recipients = [group_id]
    else:
        chat_key = f"dm:{source_uuid}"
        chat_label = f"1:1-Chat mit {source_name or source_number or source_uuid}"
        recipients = [source_uuid]
    attachments = [
        {
            "id": att.get("id"),
            "content_type": att.get("contentType") or "application/octet-stream",
            "filename": att.get("filename") or f"anhang-{index}",
            "size": att.get("size") or 0,
        }
        for index, att in enumerate(dm.get("attachments") or [])
        if att.get("id")
    ]
    return {
        "key": f"{env.get('timestamp', 0)}:{source_uuid}:{dm.get('timestamp', 0)}",
        "chat_key": chat_key,
        "chat_label": chat_label,
        "sender_uuid": source_uuid,
        "sender_number": source_number,
        "sender_name": source_name or source_number or source_uuid or "unbekannt",
        "text": dm.get("message") or "",
        "attachments": attachments,
        "recipients": recipients,
    }


async def receive() -> list[dict]:
    """ Holt neue Nachrichten von der Signal-REST-API ab (v2/receive dequeued). """
    base, number, headers = _config()
    async with httpx.AsyncClient(timeout=RECEIVE_TIMEOUT_SECONDS) as http:
        response = await http.get(f"{base}/v2/receive/{number}", headers=headers)
        response.raise_for_status()
        entries = response.json()
    messages = []
    for entry in entries or []:
        message = normalize(entry)
        if message:
            messages.append(message)
    return messages


async def download_attachment(attachment_id: str) -> bytes:
    base, _, headers = _config()
    async with httpx.AsyncClient(timeout=120) as http:
        response = await http.get(f"{base}/v1/attachments/{attachment_id}", headers=headers)
        response.raise_for_status()
        return response.content


async def send(recipients: list[str], message: str, attachments: list[tuple[str, str, bytes]] | None = None) -> dict:
    """Sendet eine Nachricht (optional mit Anhängen) über v2/send. `attachments`: (filename, mime, bytes)."""
    base, number, headers = _config()
    headers = {**headers, "Content-Type": "application/json"}
    payload: dict = {"number": number, "recipients": recipients, "message": message}
    if attachments:
        if not message:
            payload["message"] = " "
        payload["base64_attachments"] = [
            f"data:{mime};filename={filename};base64,{base64.b64encode(data).decode()}"
            for filename, mime, data in attachments
        ]
    async with httpx.AsyncClient(timeout=120) as http:
        response = await http.post(f"{base}/v2/send", headers=headers, json=payload)
        response.raise_for_status()
        return response.json() if response.content else {}
