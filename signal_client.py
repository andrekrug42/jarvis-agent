import base64
import logging
import os
import re
import time
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

RECEIVE_TIMEOUT_SECONDS = 30
ALLOWLIST_TTL_SECONDS = 300

_effective_base: str | None = None
_allowlist_cache: "_Allowlist | None" = None
_allowlist_fetched_at: float = 0.0
_allowlist_warned = False
_owner_warned = False


def owner_dm_recipient() -> str | None:
    """Signal-UUID des Account-Inhabers für Benachrichtigungen (nur 1:1 an ihn).

    Kein Default im Code (Repo ist öffentlich) – nur per JARVIS_OWNER_UUID setzen.
    """
    global _owner_warned
    value = os.environ.get("JARVIS_OWNER_UUID", "").strip()
    if not value and not _owner_warned:
        _owner_warned = True
        logger.warning("JARVIS_OWNER_UUID ist nicht gesetzt – Owner-DMs (Postfach/Fail-closed/Fehler) werden nicht versendet")
    return value or None


class _Allowlist:
    """Erlaubte Chats aus JARVIS_ALLOWED_CHATS (dm:<uuid|nummer>, group:<id|name>, uuid, +nummer oder Personen-/Gruppenname)."""

    def __init__(self, allow_all: bool, dm_ids: set[str], group_ids: set[str], group_names: set[str]):
        self.allow_all = allow_all
        self.dm_ids = dm_ids
        self.group_ids = group_ids
        self.group_names = group_names

    def allows(self, *, sender_uuid: str, sender_number: str, group_id: str, group_name: str) -> bool:
        if self.allow_all:
            return True
        if group_id:
            return group_id in self.group_ids or (bool(group_name) and group_name.lower() in self.group_names)
        sender_number = _norm_phone(sender_number)
        return (bool(sender_uuid) and sender_uuid in self.dm_ids) or (bool(sender_number) and sender_number in self.dm_ids)


def _norm_phone(value: str) -> str:
    """Telefonnummern vergleichbar machen: Leerzeichen raus ('+49 151 ...' == '+49151...').

    UUIDs und Namen bleiben unveraendert (nur Werte mit fuehrendem '+' werden normalisiert).
    """
    value = (value or "").strip()
    if value.startswith("+"):
        return re.sub(r"\s+", "", value)
    return value


def _clean_entry(value: str) -> str:
    """Allowlist-Eintrag robust saeubern: aeussere Whitespace UND Anfuehrungszeichen raus
    (Secret-Werte werden gern als '\"...\"' uebernommen), Telefonnummern ohne Leerzeichen."""
    return _norm_phone(value.strip().strip("\"'").strip())


def _allowlist_entries() -> list[str]:
    raw = os.environ.get("JARVIS_ALLOWED_CHATS", "").strip().strip("\"'").strip()
    if not raw:
        return []
    entries = []
    for part in raw.split(","):
        part = _clean_entry(part)
        if part:
            entries.append(part)
    return entries


def _name_matches(field: str, lowered: str) -> bool:
    # "melina" matcht "Melina", "Melina Krug", "Krug, Melina" – exakt, als Wort oder als Praefix
    field = (field or "").strip().lower()
    if not field or not lowered:
        return False
    if field == lowered or field.startswith(lowered):
        return True
    return lowered in field.replace(",", " ").split()


async def _list_json(path: str, base: str, headers: dict[str, str], what: str) -> list[dict]:
    try:
        response = await _request("GET", path, base, headers, 30)
        data = response.json()
        return data if isinstance(data, list) else []
    except Exception as error:
        logger.warning("Allowlist: %s konnten nicht geladen werden (%s) – Namen werden nicht aufgeloest", what, str(error)[:200])
        return []


async def _notify_owner_allowlist_empty() -> None:
    """Einmalige DM an den Account-Inhaber, wenn die Allowlist leer ist (fail-closed)."""
    owner = owner_dm_recipient()
    if not owner:
        return
    try:
        base, number, headers = _config()
        headers = {**headers, "Content-Type": "application/json"}
        await _request(
            "POST",
            "/v2/send",
            base,
            headers,
            30,
            json={
                "number": number,
                "recipients": [owner],
                "message": "⚠️ JARVIS_ALLOWED_CHATS ist leer – fail-closed aktiv: alle Chats werden blockiert, bis die Allowlist gesetzt ist.",
            },
        )
    except Exception as error:
        logger.warning("Owner-DM (Allowlist leer) fehlgeschlagen: %s", str(error)[:200])


async def _build_allowlist() -> _Allowlist:
    global _allowlist_cache, _allowlist_fetched_at, _allowlist_warned
    now = time.monotonic()
    if _allowlist_cache is not None and now - _allowlist_fetched_at < ALLOWLIST_TTL_SECONDS:
        return _allowlist_cache
    entries = _allowlist_entries()
    if not entries:
        # fail-closed: ohne Allowlist wird ALLES blockiert (nie fail-open), Owner wird per DM informiert
        if not _allowlist_warned:
            logger.warning("JARVIS_ALLOWED_CHATS ist nicht gesetzt – fail-closed aktiv, ALLE Chats blockiert")
            _allowlist_warned = True
            await _notify_owner_allowlist_empty()
        allowlist = _Allowlist(False, set(), set(), set())
    else:
        base, number, headers = _config()
        contacts = await _list_json(f"/v1/contacts/{number}", base, headers, "Kontakte")
        groups = await _list_json(f"/v1/groups/{number}", base, headers, "Gruppen")
        dm_ids: set[str] = set()
        group_ids: set[str] = set()
        group_names: set[str] = set()
        unresolved: list[str] = []
        for entry in entries:
            lowered = entry.lower()
            if entry.startswith("dm:"):
                dm_ids.add(_norm_phone(entry[3:].strip()))
                continue
            if entry.startswith("group:"):
                rest = entry[6:].strip()
                group_ids.add(rest)
                group_names.add(rest.lower())
                continue
            dm_ids.add(_norm_phone(entry))
            matched = False
            for contact in contacts:
                if any(
                    _name_matches(str(contact.get(field) or ""), lowered)
                    for field in ("name", "given_name", "profile_name", "number", "username")
                ):
                    matched = True
                    if contact.get("uuid"):
                        dm_ids.add(str(contact["uuid"]))
                    if contact.get("number"):
                        dm_ids.add(_norm_phone(str(contact["number"])))
            for group in groups:
                group_name = str(group.get("name") or "").strip().lower()
                if group_name and _name_matches(group_name, lowered):
                    matched = True
                    for field in ("id", "internal_id"):
                        if group.get(field):
                            group_ids.add(str(group[field]))
                    group_names.add(group_name)
            if not matched and not entry.startswith("+") and not ("-" in entry and len(entry) == 36):
                unresolved.append(entry)
        if unresolved:
            logger.warning("Allowlist: Eintraege ohne passenden Kontakt/Gruppe: %s", ", ".join(unresolved))
        logger.info(
            "Allowlist aktiv: %d DM-IDs, %d Gruppen-IDs, Gruppennamen=%s",
            len(dm_ids),
            len(group_ids),
            sorted(group_names),
        )
        allowlist = _Allowlist(False, dm_ids, group_ids, group_names)
    _allowlist_cache = allowlist
    _allowlist_fetched_at = now
    return allowlist


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


def _base_candidates(base: str) -> list[str]:
    # Falls der Reverse-Proxy das Pfad-Präfix nicht durchreicht, nochmal ohne Pfad versuchen.
    candidates = [base]
    if "://" in base:
        scheme, rest = base.split("://", 1)
        if "/" in rest:
            candidates.append(f"{scheme}://{rest.split('/', 1)[0]}")
    return candidates


async def _request(method: str, path: str, base: str, headers: dict[str, str], timeout: float, **kwargs) -> httpx.Response:
    global _effective_base
    bases = [_effective_base] if _effective_base else _base_candidates(base)
    response = None
    for candidate in bases:
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.request(method, f"{candidate}{path}", headers=headers, **kwargs)
        if response.status_code != 404:
            _effective_base = candidate
            break
    assert response is not None
    if response.is_error:
        raise httpx.HTTPStatusError(
            f"{response.status_code} for url {response.url}: {response.text[:500]}",
            request=response.request,
            response=response,
        )
    return response


def _allowed_sources() -> set[str] | None:
    raw = os.environ.get("SIGNAL_ALLOWED_SOURCES", "").strip().strip("\"'").strip()
    if not raw:
        return None
    return {_clean_entry(part) for part in raw.split(",") if part.strip()}


def _is_own_message(env: dict, own_number: str) -> bool:
    source_number = env.get("sourceNumber") or env.get("source") or ""
    return bool(source_number) and _norm_phone(source_number) == _norm_phone(own_number)


def normalize(entry: dict, allowlist: _Allowlist | None = None) -> dict | None:
    """Formt einen receive-Envelope in ein flaches Nachrichten-Dict um (v1: nackter Envelope, v2: {"envelope": ...}). None = ignorieren (z. B. Receipts, eigene Nachrichten, nicht erlaubte Chats)."""
    env = entry.get("envelope") or entry
    dm = env.get("dataMessage")
    if not dm:
        return None
    _, own_number, _ = _config()
    if _is_own_message(env, own_number):
        return None
    source_uuid = env.get("sourceUuid") or env.get("source") or ""
    source_number = _norm_phone(env.get("sourceNumber") or env.get("source") or "")
    source_name = env.get("sourceName") or ""
    allowed = _allowed_sources()
    if allowed is not None and source_uuid not in allowed and source_number not in allowed:
        return None
    group_info = dm.get("groupInfo") or {}
    group_id = group_info.get("groupId") or ""
    group_name = group_info.get("name") or ""
    if allowlist is not None and not allowlist.allows(
        sender_uuid=source_uuid, sender_number=source_number, group_id=group_id, group_name=group_name
    ):
        return None
    if group_id:
        # Empfangene groupId ist die interne ID (base64 der rohen 32 Bytes, einfach kodiert).
        # Senden/Typing/Receipts erwarten "group." + base64(internalId) (doppelt base64, siehe getRecipientType in signal-cli-rest-api).
        if group_id.startswith("group."):
            group_recipient = group_id
        else:
            group_recipient = "group." + base64.b64encode(group_id.encode()).decode()
        chat_key = f"group:{group_id}"
        chat_label = f'Gruppenchat "{group_name}"' if group_name else f"Gruppe {group_id}"
        recipients = [group_recipient]
        receipt_targets = [group_recipient]
    else:
        chat_key = f"dm:{source_uuid}"
        chat_label = f"1:1-Chat mit {source_name or source_number or source_uuid}"
        recipients = [source_uuid]
        receipt_targets = [source_number, source_uuid]
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
        "timestamp": dm.get("timestamp") or env.get("timestamp") or 0,
        "chat_key": chat_key,
        "chat_label": chat_label,
        "sender_uuid": source_uuid,
        "sender_number": source_number,
        "sender_name": source_name or source_number or source_uuid or "unbekannt",
        "text": dm.get("message") or "",
        "attachments": attachments,
        "recipients": recipients,
        "receipt_recipients": [r for r in receipt_targets if r],
    }


async def receive() -> list[dict]:
    """Holt neue Nachrichten von der Signal-REST-API ab (v1/receive dequeued).

    Read-Receipts werden hier NICHT automatisch gesendet (send_read_receipts=false);
    die Bridge quittiert nur Nachrichten aus erlaubten Chats einzeln (send_read_receipt).
    """
    base, number, headers = _config()
    allowlist = await _build_allowlist()
    response = await _request("GET", f"/v1/receive/{number}?send_read_receipts=false", base, headers, RECEIVE_TIMEOUT_SECONDS)
    entries = response.json()
    messages = []
    dropped = 0
    for entry in entries or []:
        message = normalize(entry, allowlist)
        if message:
            messages.append(message)
        elif (entry.get("envelope") or entry).get("dataMessage"):
            dropped += 1
    if dropped:
        logger.info("receive: %d Nachricht(en) verworfen (Chat nicht in JARVIS_ALLOWED_CHATS)", dropped)
    return messages


async def download_attachment(attachment_id: str) -> bytes:
    base, _, headers = _config()
    response = await _request("GET", f"/v1/attachments/{quote(attachment_id, safe='')}", base, headers, 120)
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
            f"data:{mime};filename={filename};base64,{base64.b64encode(data).decode()}" for filename, mime, data in attachments
        ]
    response = await _request("POST", "/v2/send", base, headers, 120, json=payload)
    return response.json() if response.content else {}


async def send_typing(recipients: list[str], stop: bool = False) -> None:
    """Zeigt den Schreib-Indikator im Chat (PUT /v1/typing-indicator) bzw. blendet ihn aus (DELETE)."""
    base, number, headers = _config()
    headers = {**headers, "Content-Type": "application/json"}
    method = "DELETE" if stop else "PUT"
    for recipient in recipients:
        await _request(method, f"/v1/typing-indicator/{number}", base, headers, 30, json={"recipient": recipient})


async def send_read_receipt(recipient: str, timestamp: int) -> None:
    """Markiert eine empfangene Nachricht als gelesen (POST /v1/receipts, receipt_type=read)."""
    base, number, headers = _config()
    headers = {**headers, "Content-Type": "application/json"}
    payload = {"receipt_type": "read", "recipient": recipient, "timestamp": timestamp}
    await _request("POST", f"/v1/receipts/{number}", base, headers, 30, json=payload)


def _group_target(group: dict, allowlist: "_Allowlist") -> tuple[list[str], str, str]:
    gid = str(group.get("id") or "")
    internal = str(group.get("internal_id") or "")
    gname = str(group.get("name") or "")
    if not (
        allowlist.allows(sender_uuid="", sender_number="", group_id=gid, group_name=gname)
        or allowlist.allows(sender_uuid="", sender_number="", group_id=internal, group_name=gname)
    ):
        raise ValueError(f"Gruppe '{gname or gid or internal}' ist nicht in JARVIS_ALLOWED_CHATS erlaubt")
    if gid.startswith("group."):
        recipient = gid
    else:
        recipient = "group." + base64.b64encode((internal or gid).encode()).decode()
    label = f'Gruppenchat "{gname}"' if gname else f"Gruppe {gid}"
    return [recipient], f"group:{internal or gid}", label


async def resolve_target(target: str) -> tuple[list[str], str, str]:
    """Löst ein Sendeziel in v2/send-Recipients auf und prüft die Allowlist (JARVIS_ALLOWED_CHATS).

    `target`: '+nummer', UUID, Kontaktname/Vorname (über /v1/contacts), 'gruppe'/'group:<name|id>'
    oder ein Gruppenname (über /v1/groups).
    Gibt (recipients, chat_key, label) zurück; wirft ValueError, wenn das Ziel unbekannt oder nicht erlaubt ist.
    """
    base, number, headers = _config()
    allowlist = await _build_allowlist()
    target = _clean_entry(target or "")
    lowered = target.lower()
    if not target:
        raise ValueError("leeres Ziel")
    if target.startswith("dm:"):
        target = _norm_phone(target[3:].strip())
        lowered = target.lower()
    if target.startswith("+") or ("-" in target and len(target) == 36):
        is_uuid = "-" in target
        target = _norm_phone(target)
        if not allowlist.allows(
            sender_uuid=target if is_uuid else "",
            sender_number="" if is_uuid else target,
            group_id="",
            group_name="",
        ):
            raise ValueError(f"Ziel {target} ist nicht in JARVIS_ALLOWED_CHATS erlaubt")
        return [target], f"dm:{target}", f"1:1-Chat mit {target}"
    if target.startswith("group:"):
        target = target[6:].strip()
        lowered = target.lower()
    contacts = await _list_json(f"/v1/contacts/{number}", base, headers, "Kontakte")
    groups = await _list_json(f"/v1/groups/{number}", base, headers, "Gruppen")
    if lowered in ("gruppe", "group", "alle", "all", "gruppenchat"):
        if len(groups) == 1:
            return _group_target(groups[0], allowlist)
        names = [str(g.get("name") or g.get("id") or "?") for g in groups]
        raise ValueError(f"mehrere Gruppen gefunden ({', '.join(names)}) – bitte den Gruppennamen angeben")
    for contact in contacts:
        if any(
            _name_matches(str(contact.get(field) or ""), lowered) for field in ("name", "given_name", "profile_name", "number", "username")
        ):
            uuid = str(contact.get("uuid") or "")
            contact_number = _norm_phone(str(contact.get("number") or ""))
            recipient = uuid or contact_number
            if not recipient:
                raise ValueError(f"Kontakt '{target}' hat weder UUID noch Nummer")
            if not allowlist.allows(sender_uuid=uuid, sender_number=contact_number, group_id="", group_name=""):
                raise ValueError(f"Kontakt '{target}' ({recipient}) ist nicht in JARVIS_ALLOWED_CHATS erlaubt")
            name = str(contact.get("name") or contact.get("given_name") or contact.get("profile_name") or recipient)
            return [recipient], f"dm:{uuid or contact_number}", f"1:1-Chat mit {name}"
    for group in groups:
        gname = str(group.get("name") or "").strip().lower()
        gid = str(group.get("id") or "").lower()
        ginternal = str(group.get("internal_id") or "").lower()
        if lowered and (lowered in (gid, ginternal) or _name_matches(gname, lowered)):
            return _group_target(group, allowlist)
    raise ValueError(f"kein Kontakt oder Gruppe namens '{target}' gefunden")
