import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import mistralai.workflows as workflows
from pydantic import BaseModel

logger = logging.getLogger(__name__)

LOG_LIMIT = 200
PROCESSED_LIMIT = 500
LOG_CONTEXT_ENTRIES = 30
DEFAULT_MAILBOX_INTERVAL_SECONDS = 3600
BUILD_SHA_TTL_SECONDS = 60
DEFAULT_MAILBOX_MAX_MESSAGES = 10
MAILBOX_PROMPT = (
    "[POSTFACH-CHECK] Pr\u00fcfe jetzt den Posteingang des JARVIS-Gmail-Kontos. "
    'Lies zuerst das Regeln-Dokument \u201eJARVIS-Verhaltensregeln.md" (Library \u201eAllgemeine Dokumente") mit search_knowledge '
    "(Gmail-Query, Snippet-first, Familien-Auto-Replies, unbekannte Absender, Anh\u00e4nge). "
    "Unbearbeitete Mails im Pr\u00fcf-Scope: maximal {max_messages}. "
    'Beginne deine Antwort IMMER mit einer eigenen ersten Zeile im exakten Format "MAILS: <Anzahl>" '
    '(z. B. "MAILS: 0"). Nur bei MAILS > 0 schickt die Bridge die folgende Nachricht als Signal-DM an Andre; '
    "danach eine kurze, nat\u00fcrliche Nachricht im Signal-Stil (pro Mail 1\u20132 S\u00e4tze: wer, worum geht's, was wurde gemacht)."
)

_agent_registered = False
_build_sha_cache: str | None = None
_build_sha_fetched_at: float = 0.0

# Agent-Health-Guard: /v1/receive dequeued destructiv — bei "down" wird NICHT gepollt,
# damit Nachrichten in der Signal-Queue bleiben (kein Verlust).
CONTROL_API_BASE = "https://api.mistral.ai"
AGENT_PROBE_TTL_SECONDS = 45
AGENT_PROBE_SESSION_KEY = "jarvis-health-probe"
DEFAULT_AGENT_RESTART_INTERVAL_SECONDS = 4 * 3600  # praeventiver Restart des Agent-Deployments, 0 = aus
PLANNED_RESTART_WINDOW_SECONDS = 420
POLL_TIMEOUT_DM_MIN_INTERVAL_SECONDS = 1800

_agent_probe_ok: bool | None = None
_agent_probe_at: float = 0.0
_agent_probe_error: str = ""
_poll_timeout_dm_at: float = 0.0


def _owner_dm_recipient() -> str | None:
    """Signal-UUID des Account-Inhabers für Benachrichtigungen (nur 1:1 an ihn)."""
    import signal_client

    return signal_client.owner_dm_recipient()


def _control_api_key() -> str:
    key = os.environ.get("JARVIS_AGENT_API_KEY", "").strip()
    if key:
        return key
    token_path = os.environ.get("MISTRAL_SA_TOKEN_PATH", "")
    if token_path and os.path.exists(token_path):
        try:
            with open(token_path, encoding="utf-8") as handle:
                return handle.read().strip()
        except Exception:
            return ""
    return ""


def _agent_deployment_name() -> str:
    return os.environ.get("JARVIS_DEPLOYMENT_NAME", "").strip() or os.environ.get("JARVIS_AGENT_NAME", "jarvis")


def _is_agent_down_error(error: BaseException) -> bool:
    text = str(error)
    return "no worker serves" in text or "409 Conflict" in text


async def _probe_agent_via_session() -> tuple[bool, str]:
    from mistralai import agents

    agent_name = os.environ.get("JARVIS_AGENT_NAME", "jarvis")
    user_api_key = os.environ.get("JARVIS_AGENT_API_KEY", "").strip()
    try:
        if user_api_key:
            await ensure_agent_registered(agent_name, user_api_key)
            agent = agents.Agent.load_from(agent_name=agent_name, api_key=user_api_key)
        else:
            key = _control_api_key()
            client = agents.Client(api_key=key) if key else agents.Client()
            agent = agents.Agent.load_from(agent_name=agent_name, client=client)
        await agent.start_session(idempotency_key=AGENT_PROBE_SESSION_KEY)
        return True, ""
    except Exception as error:
        return False, f"{type(error).__name__}: {str(error)[:200]}"


def _parse_deployment_health(data: dict[str, Any], name: str) -> tuple[bool, str]:
    """DeploymentDetailResponse: ok = is_active (mindestens ein Worker live) oder workers-Liste nicht leer."""
    if isinstance(data.get("deployment"), dict):
        data = data["deployment"]
    active = bool(data.get("is_active"))
    workers = data.get("workers") or []
    if active or len(workers) > 0:
        return True, f"is_active={active} workers={len(workers)}"
    return False, f"deployment {name}: is_active=False workers=0 last_heartbeat={data.get('last_heartbeat')}"


async def _probe_agent() -> tuple[bool, str]:
    """Read-only Probe: GET /v1/workflows/deployments/{name} -> is_active/workers. Fallback: Session-Start (409 = down)."""
    import httpx

    key = _control_api_key()
    if not key:
        return await _probe_agent_via_session()
    name = _agent_deployment_name()
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(f"{CONTROL_API_BASE}/v1/workflows/deployments/{name}", headers={"Authorization": f"Bearer {key}"})
        if response.status_code == 200:
            return _parse_deployment_health(response.json(), name)
        if response.status_code in (401, 403):
            return await _probe_agent_via_session()
        return False, f"GET deployment -> {response.status_code}: {response.text[:160]}"
    except Exception:
        return await _probe_agent_via_session()


async def _notify_owner_poll_timeout() -> None:
    """ReadTimeout bei /v1/receive: serverseitig könnten Envelopes dequeued worden sein, ohne dass wir sie sahen."""
    import time

    import signal_client

    global _poll_timeout_dm_at
    now = time.time()
    if now - _poll_timeout_dm_at < POLL_TIMEOUT_DM_MIN_INTERVAL_SECONDS:
        return
    _poll_timeout_dm_at = now
    owner = _owner_dm_recipient()
    if not owner:
        return
    try:
        await signal_client.send(
            [owner],
            "⚠️ Signal-Poll (receive) ist per Timeout fehlgeschlagen — serverseitig könnten Nachrichten entnommen worden sein, "
            "ohne dass ich sie verarbeiten konnte. Falls eine Nachricht fehlt: bitte erneut senden.",
        )
    except Exception:
        pass


async def _poll_signal() -> list[dict[str, Any]]:
    """receive() darf den Run nie fehlschlagen lassen — bei Fehler leer zurück (Nachrichten bleiben serverseitig in der Queue)."""
    import signal_client

    try:
        return await signal_client.receive()
    except Exception as error:
        logger.warning("receive() fehlgeschlagen (%s): %s — Run läuft ohne Poll weiter", type(error).__name__, str(error)[:300])
        if "timeout" in str(error).lower():
            await _notify_owner_poll_timeout()
        return []


async def _agent_health(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """TTL-gecachter Health-Check des Agent-Workers + einmalige Owner-DMs bei Down/Recovery."""
    import time

    import signal_client
    import state_store

    global _agent_probe_ok, _agent_probe_at, _agent_probe_error
    now = time.time()
    if _agent_probe_ok is not None and now - _agent_probe_at < AGENT_PROBE_TTL_SECONDS:
        ok, err = _agent_probe_ok, _agent_probe_error
    else:
        ok, err = await _probe_agent()
        _agent_probe_ok, _agent_probe_at, _agent_probe_error = ok, now, err
    agent_state = state.setdefault("agent", {})
    was_down = bool(agent_state.get("down"))
    planned_until = float(agent_state.get("planned_restart_until") or 0)
    result: dict[str, Any] = {"key": "agent", "status": "ok" if ok else "down"}
    changed = was_down != (not ok)
    if not ok:
        result["error"] = err[:300]
        agent_state["down"] = True
        agent_state["down_since"] = agent_state.get("down_since") or now
        if changed:
            logger.warning("agent-down: %s", err[:300])
        if not agent_state.get("down_notified") and now >= planned_until:
            agent_state["down_notified"] = True
            changed = True
            owner = _owner_dm_recipient()
            if owner:
                try:
                    await signal_client.send(
                        [owner],
                        f"⚠️ JARVIS-Agent-Worker nicht erreichbar ({err[:160]}). Ich halte den Signal-Empfang an, "
                        "damit keine Nachrichten verloren gehen, und prüfe weiter. Mailbox/Tasks sind pausiert.",
                    )
                except Exception:
                    pass
    else:
        if was_down:
            logger.info("agent wieder erreichbar")
            changed = True
            owner = _owner_dm_recipient()
            if owner:
                try:
                    await signal_client.send([owner], "✅ JARVIS-Agent wieder erreichbar — wartende Nachrichten werden jetzt verarbeitet.")
                except Exception:
                    pass
        agent_state["down"] = False
        agent_state.pop("down_notified", None)
        agent_state.pop("down_since", None)
    if changed:
        try:
            await state_store.save(state)
        except Exception:
            pass
    return state, result


def _agent_restart_interval() -> int:
    try:
        return int(os.environ.get("JARVIS_AGENT_RESTART_INTERVAL_SECONDS", "") or DEFAULT_AGENT_RESTART_INTERVAL_SECONDS)
    except ValueError:
        return DEFAULT_AGENT_RESTART_INTERVAL_SECONDS


async def _restart_agent_deployment() -> tuple[bool, str]:
    import httpx

    key = _control_api_key()
    if not key:
        return False, "kein API-Key für Control-API"
    name = _agent_deployment_name()
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(
                f"{CONTROL_API_BASE}/v1/workflows/deployments/{name}/restart", headers={"Authorization": f"Bearer {key}"}
            )
        if response.status_code in (200, 201, 202):
            return True, response.text[:160]
        return False, f"POST restart -> {response.status_code}: {response.text[:160]}"
    except Exception as error:
        return False, f"{type(error).__name__}: {str(error)[:160]}"


async def _agent_restart_check(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Präventiver Restart des Agent-Deployments (default alle 4h), damit eine abgestorbene Instanz nicht lange unbemerkt bleibt."""
    import time

    import state_store

    interval = _agent_restart_interval()
    if interval <= 0:
        return state, None
    now = time.time()
    last = float(state.get("agent_restart_last") or 0)
    if not last:
        # Erster Lauf nach Bridge-Redeploy: Timer neu starten statt sofort zu restarten.
        state["agent_restart_last"] = now
        try:
            await state_store.save(state)
        except Exception:
            pass
        return state, None
    if now - last < interval:
        return state, None
    state["agent_restart_last"] = now
    state.setdefault("agent", {})["planned_restart_until"] = now + PLANNED_RESTART_WINDOW_SECONDS
    try:
        await state_store.save(state)
    except Exception:
        pass
    ok, msg = await _restart_agent_deployment()
    if ok:
        logger.info("geplanter Agent-Restart ausgeführt (%s): %s", _agent_deployment_name(), msg)
    else:
        logger.warning("geplanter Agent-Restart fehlgeschlagen: %s", msg)
    return state, {"key": "agent-restart", "status": "ok" if ok else "error", "error": msg[:300]}


async def _process_message_guarded(message: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """409/no-worker abfangen: Nachricht war schon dequeued -> Owner-DM mit Inhalt, kein Retry-Sturm."""
    import time

    global _agent_probe_ok, _agent_probe_at, _agent_probe_error
    try:
        return await _process_message(message, state)
    except Exception as error:
        if not _is_agent_down_error(error):
            raise
        _agent_probe_ok, _agent_probe_at, _agent_probe_error = False, time.time(), str(error)[:300]
        state.setdefault("agent", {})["down"] = True
        logger.warning("process_message %s: agent down (%s)", message.get("key"), str(error)[:200])
        owner = _owner_dm_recipient()
        if owner:
            try:
                import signal_client

                preview = (message.get("text") or "").strip()[:200] or "(nur Anhänge)"
                await signal_client.send(
                    [owner],
                    f"⚠️ Nachricht von {message.get('sender_name', '?')} konnte nicht verarbeitet werden (Agent-Worker down, 409). "
                    f"Inhalt: „{preview}“ — bitte ggf. erneut senden.",
                )
            except Exception:
                pass
        detail = {"key": message.get("key"), "status": "agent-down", "error": str(error)[:200], "chat": message.get("chat_key")}
        return detail, state


def _mailbox_settings() -> tuple[int, int]:
    try:
        interval = int(os.environ.get("JARVIS_MAILBOX_INTERVAL_SECONDS", "") or DEFAULT_MAILBOX_INTERVAL_SECONDS)
    except ValueError:
        interval = DEFAULT_MAILBOX_INTERVAL_SECONDS
    try:
        max_messages = int(os.environ.get("JARVIS_MAILBOX_MAX_MESSAGES", "") or DEFAULT_MAILBOX_MAX_MESSAGES)
    except ValueError:
        max_messages = DEFAULT_MAILBOX_MAX_MESSAGES
    return interval, max_messages


def _parse_mail_count(preview: str) -> int | None:
    import re

    match = re.search(r"MAILS:\s*(\d+)", preview)
    return int(match.group(1)) if match else None


def _strip_mail_marker(preview: str) -> str:
    import re

    return re.sub(r"^MAILS:\s*\d+\s*[-–—:]?\s*", "", preview, count=1).strip()


async def _merge_agent_writes(state: dict[str, Any]) -> None:
    # Der Agent schreibt waehrend _process_message (send_signal_message/schedule_task) direkt
    # in den State (eigener load+save). Unser lokaler Snapshot ist danach aelter und wuerde diese
    # Aenderungen beim naechsten save wieder loeschen. Deshalb vor dem save die frischen
    # outbox/tasks-Eintraege uebernehmen; bei bekannten IDs gewinnt der lokale Stand
    # (z. B. Task-Status "done", den wir nach dem Agenten-Lauf setzen).
    import state_store

    try:
        fresh = await state_store.load()
    except Exception:
        return
    for key in ("outbox", "tasks"):
        fresh_list = fresh.get(key)
        if not isinstance(fresh_list, list):
            continue
        local_list = state.get(key)
        if not isinstance(local_list, list):
            state[key] = fresh_list
            continue
        local_by_id = {item.get("id"): item for item in local_list if isinstance(item, dict)}
        merged: list[Any] = []
        seen: set[Any] = set()
        for item in fresh_list:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            merged.append(local_by_id.get(item_id, item))
            seen.add(item_id)
        for item in local_list:
            if isinstance(item, dict) and item.get("id") not in seen:
                merged.append(item)
        state[key] = merged


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


async def _current_build_sha() -> str | None:
    """HEAD-Sha des Agent-Repos von GitHub, mit kurzem In-Memory-Cache (TTL BUILD_SHA_TTL_SECONDS).

    Spart pro Nachricht einen HTTP-Call (und bei langsamem GitHub den 15-s-Stall). Bei Fehler
    wird der zuletzt bekannte Cache-Wert zurueckgegeben (sonst None).
    """
    global _build_sha_cache, _build_sha_fetched_at
    import time

    now = time.time()
    if _build_sha_cache is not None and now - _build_sha_fetched_at < BUILD_SHA_TTL_SECONDS:
        return _build_sha_cache
    try:
        import httpx

        repo = os.environ.get("JARVIS_AGENT_REPO", "andrekrug42/jarvis-agent")
        branch = os.environ.get("JARVIS_AGENT_BRANCH", "main")
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(f"https://api.github.com/repos/{repo}/commits/{branch}")
        sha = resp.json().get("sha") if resp.status_code == 200 else None
    except Exception:
        return _build_sha_cache
    if sha:
        _build_sha_cache = sha
        _build_sha_fetched_at = now
    return sha


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
    lines.append(f"Chat-Key: {message['chat_key']}")
    now_utc = datetime.now(UTC)
    berlin = now_utc.astimezone(ZoneInfo("Europe/Berlin"))
    lines.append(f"Aktuelle Zeit: {berlin.strftime('%Y-%m-%d %H:%M')} Europe/Berlin ({now_utc.strftime('%Y-%m-%d %H:%M')} UTC)")
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


def _as_plain(obj: Any) -> Any:
    """Dataclasses (z. B. SDK-Events/Records) rekursiv nach dict/list aufloesen, sonst unveraendert."""
    if is_dataclass(obj) and not isinstance(obj, type):
        try:
            return asdict(obj)
        except Exception:
            return obj
    return obj


def extract_file_ids(obj: Any, found: list[str] | None = None) -> list[str]:
    """Sammelt rekursiv alle file_id-Werte (z. B. generierte Bilder des generate_image-Tools)."""
    if found is None:
        found = []
    obj = _as_plain(obj)
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


def _is_trusted_image_url(url: str) -> bool:
    """Nur Mistral-Hosts duerfen serverseitig geladen werden (SSRF-Schutz)."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host == "mistral.ai" or host.endswith(".mistral.ai")


def extract_image_urls(obj: Any, found: list[str] | None = None) -> list[str]:
    """Sammelt rekursiv alle image_url-Werte (z. B. Ergebnis des Tools generate_image)."""
    if found is None:
        found = []
    obj = _as_plain(obj)
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


def _image_payload_from_event(event: Any) -> Any | None:
    """structured_content eines Tool-Result-Events (EntryCompleted), wenn es ein Bild traegt.

    Der Completed-Record enthaelt nur tool_calls (Name+Argumente), NICHT die Ergebnisse -
    deshalb wird das Bild ueber die Session-Events (on_event) eingesammelt.
    """
    try:
        entry = getattr(event, "entry", None)
        if entry is None:
            return None
        structured = getattr(entry, "structured_content", None)
        if structured is None:
            return None
        dump = _as_plain(structured)
        if isinstance(dump, dict) and ("image_url" in dump or "file_id" in dump):
            return dump
    except Exception:
        return None
    return None


@workflows.activity(start_to_close_timeout=timedelta(seconds=60), retry_policy_max_attempts=2)
async def poll_signal_activity() -> list[dict[str, Any]]:
    return await _poll_signal()


async def _process_message(message: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
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

    if message["key"] in state.get("processed", []):
        return {"key": message["key"], "status": "already_processed"}, state

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
            files_info.append({"filename": attachment["filename"], "content_type": attachment["content_type"], "file_id": file_id})
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
        if entry.get("chat") != message["chat_label"]  # nur ANDERE Chats ins Cross-Chat-Gedaechtnis
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
        build_sha = await _current_build_sha()
        if build_sha:
            stale = chat_state.get("agent_build") != build_sha and (chat_state.get("agent_build") or chat_state.get("session_key"))
            if stale:
                chat_state["session_epoch"] = int(chat_state.get("session_epoch", 0)) + 1
                chat_state["session_key"] = f"signal-chat:{message['chat_key']}:e{chat_state['session_epoch']}"
            chat_state["agent_build"] = build_sha
    except Exception:
        pass
    session_key = chat_state.get("session_key") or f"signal-chat:{message['chat_key']}"
    try:
        session = await agent.start_session(idempotency_key=session_key)
    except Exception as error:
        if _is_agent_down_error(error):
            raise  # 409 no worker: Epoch-Retry wuerde sofort wieder 409 liefern
        if user_api_key:
            _agent_registered = False
            await ensure_agent_registered(agent_name, user_api_key)
        chat_state["session_epoch"] = int(chat_state.get("session_epoch", 0)) + 1
        session_key = f"signal-chat:{message['chat_key']}:e{chat_state['session_epoch']}"
        chat_state["session_key"] = session_key
        session = await agent.start_session(idempotency_key=session_key)

    import asyncio

    turn_image_payloads: list[Any] = []
    events_seen = 0

    def _collect_event(event: Any) -> None:
        # Der Completed-Record enthaelt nur tool_calls (Name+Argumente), nicht die Ergebnisse -
        # Bild-URLs/file_ids kommen ueber die Session-Events (on_event) als ToolResultEntry.
        nonlocal events_seen
        events_seen += 1
        payload = _image_payload_from_event(event)
        if payload is not None:
            turn_image_payloads.append(payload)

    async def _run_with_retry(sess, turn_key: str):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await sess.run(envelope, idempotency_key=turn_key, on_event=_collect_event)
            except Exception as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
        raise last_error

    try:
        record = await _run_with_retry(session, f"signal-msg:{message['key']}")
    except Exception as error:
        if _is_agent_down_error(error):
            raise
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
        record = await record.turn.result(on_event=_collect_event)

    reply_text = (getattr(record, "text", "") or "").strip()
    extraction_sources = [record, *turn_image_payloads]
    generated_file_ids = extract_file_ids(extraction_sources)
    image_urls = extract_image_urls(extraction_sources)
    logger.info(
        "bild-check key=%s: events=%d payloads=%d file_ids=%d urls=%d",
        message["key"],
        events_seen,
        len(turn_image_payloads),
        len(generated_file_ids),
        len(image_urls),
    )

    attachments_out: list[tuple[str, str, bytes]] = []
    for index, file_id in enumerate(generated_file_ids, 1):
        try:
            data = await state_store.download_mistral_file(file_id)
            attachments_out.append((f"jarvis-bild-{index}.png", "image/png", data))
        except Exception as error:
            logger.warning("bild per file_id nicht ladbar (file_id=%s): %s", file_id, error)
    for url in image_urls:
        if not _is_trusted_image_url(url):
            logger.warning("image-url nicht vertraut, uebersprungen: %s", url)
            continue
        try:
            import httpx

            async with httpx.AsyncClient(timeout=120) as http:
                response = await http.get(url)
                response.raise_for_status()
            mime = response.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
            attachments_out.append((f"jarvis-bild-{len(attachments_out) + 1}.{ext}", mime, response.content))
        except Exception as error:
            logger.warning("bild per url nicht ladbar (url=%s): %s", url, error)

    if (reply_text or attachments_out) and message.get("recipients"):
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
                        await state_store.upload_document_to_library(library_id, attachment["filename"], attachment["content_type"], data)
                        library_name = next((lib["name"] for lib in libraries if lib["id"] == library_id), library_id)
                        filed_notes.append(
                            f"Dokument '{attachment['filename']}' in Library '{library_name}' abgelegt ({decision.get('reason', '')})"
                        )
                except Exception:
                    pass
        except Exception:
            pass

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    summary = _truncate(
        f"{message['text'] or '(Anhang)'} -> {reply_text or '(Bild)'}" + (" | " + "; ".join(filed_notes) if filed_notes else "")
    )
    state.setdefault("log", []).append({"ts": now, "chat": message["chat_label"], "sender": message["sender_name"], "summary": summary})
    state["log"] = state["log"][-LOG_LIMIT:]
    state.setdefault("processed", []).append(message["key"])
    state["processed"] = state["processed"][-PROCESSED_LIMIT:]
    await _merge_agent_writes(state)
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
        "reply_preview": _truncate(reply_text, 600),
        "images": len(attachments_out),
        "filed": len(filed_notes),
    }, state


@workflows.activity(start_to_close_timeout=timedelta(seconds=60), retry_policy_max_attempts=2)
async def load_state_activity() -> dict[str, Any]:
    import state_store

    return await state_store.load()


@workflows.activity(start_to_close_timeout=timedelta(seconds=90), retry_policy_max_attempts=2)
async def agent_health_activity(state: dict[str, Any]) -> dict[str, Any]:
    state, result = await _agent_health(state)
    return {"state": state, "result": result}


@workflows.activity(start_to_close_timeout=timedelta(seconds=90), retry_policy_max_attempts=1)
async def agent_restart_check_activity(state: dict[str, Any]) -> dict[str, Any]:
    state, result = await _agent_restart_check(state)
    return {"state": state, "result": result}


@workflows.activity(start_to_close_timeout=timedelta(minutes=20), retry_policy_max_attempts=2)
async def process_message_activity(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    detail, new_state = await _process_message_guarded(message, state)
    return {"state": new_state, "result": detail}


async def _mailbox_check(state: dict[str, Any]) -> dict[str, Any]:
    import time

    import signal_client
    import state_store

    interval, max_messages = _mailbox_settings()
    now = time.time()
    last_check = float(state.get("mailbox_last_check", 0) or 0)
    if now - last_check < interval:
        return {"state": state, "result": None}
    mailbox_message = {
        "key": f"mailbox:{int(now)}",
        "timestamp": int(now * 1000),
        "chat_key": "mailbox",
        "chat_label": "Postfach (intern)",
        "sender_uuid": "",
        "sender_number": "",
        "sender_name": "System",
        "text": MAILBOX_PROMPT.format(max_messages=max_messages),
        "attachments": [],
        "recipients": [],
        "receipt_recipients": [],
    }
    detail, state = await _process_message(mailbox_message, state)
    state["mailbox_last_check"] = now
    try:
        await state_store.save(state)
    except Exception:
        pass
    owner = _owner_dm_recipient()
    preview = (detail.get("reply_preview") or "").strip()
    mail_count = _parse_mail_count(preview)
    if owner and preview and mail_count != 0:
        try:
            await signal_client.send([owner], f"📬 {_strip_mail_marker(preview)}")
        except Exception:
            pass
    return {"state": state, "result": detail}


@workflows.activity(start_to_close_timeout=timedelta(minutes=25), retry_policy_max_attempts=2)
async def mailbox_check_activity(state: dict[str, Any]) -> dict[str, Any]:
    return await _mailbox_check(state)


TASKS_MAX_PER_RUN = 3
TASK_RUNNING_STALE_SECONDS = 1800


async def _run_due_tasks(state: dict[str, Any]) -> dict[str, Any]:
    import time

    import signal_client
    import state_store

    now = time.time()
    due_tasks = []
    for task in state.get("tasks", []):
        if float(task.get("due", 0)) > now:
            continue
        if task.get("status") == "pending":
            due_tasks.append(task)
        elif task.get("status") == "running" and now - float(task.get("started", 0)) > TASK_RUNNING_STALE_SECONDS:
            task["status"] = "pending"
            due_tasks.append(task)
    due_tasks.sort(key=lambda t: float(t.get("due", 0)))
    results: list[dict[str, Any]] = []
    for task in due_tasks[:TASKS_MAX_PER_RUN]:
        result: dict[str, Any] = {
            "key": f"task:{task.get('id')}",
            "status": "error",
            "chat": task.get("target", ""),
            "task_action": (task.get("action") or "")[:120],
        }
        task["status"] = "running"
        task["started"] = now
        try:
            await state_store.save(state)
        except Exception:
            pass
        try:
            target = (task.get("target") or "").strip()
            if not target:
                raise ValueError("kein Ziel (target) in der Aufgabe hinterlegt")
            recipients, chat_key, label = await signal_client.resolve_target(target)
            message = {
                "key": f"task:{task.get('id')}",
                "timestamp": 0,
                "chat_key": chat_key,
                "chat_label": label,
                "sender_uuid": "",
                "sender_number": "",
                "sender_name": "System (zeitgesteuerte Aufgabe)",
                "text": (
                    f"[AUFGABE {task.get('due_iso') or ''}] Führe jetzt diese zeitgesteuerte Aufgabe aus: "
                    f"{task.get('action')}\n"
                    "Führe sie direkt aus (bei Mails an Familien-Adressen direkt send_email; bei einer Erinnerung "
                    "formuliere eine kurze, freundliche Nachricht). Antworte mit dem Ergebnis — es wird automatisch "
                    "in den Ziel-Chat geschickt."
                ),
                "attachments": [],
                "recipients": recipients,
                "receipt_recipients": [],
            }
            detail, state = await _process_message(message, state)
            task["status"] = "done" if detail.get("status") == "ok" else "error"
            task["finished"] = now
            task["result_preview"] = (detail.get("reply_preview") or "")[:300]
            result.update(
                {
                    "status": "ok" if task["status"] == "done" else "error",
                    "chat": chat_key,
                    "reply_preview": (detail.get("reply_preview") or "")[:200],
                }
            )
        except Exception as error:
            task["status"] = "error"
            task["finished"] = now
            task["error"] = str(error)[:300]
            result["error"] = str(error)[:200]
            try:
                owner = _owner_dm_recipient()
                if owner:
                    await signal_client.send(
                        [owner],
                        f"⚠️ Zeitgesteuerte Aufgabe fehlgeschlagen ({(task.get('action') or '')[:80]}): {str(error)[:150]}",
                    )
            except Exception:
                pass
        results.append(result)
    if results:
        state["tasks"] = [
            t
            for t in state.get("tasks", [])
            if t.get("status") in ("pending", "running") or now - float(t.get("finished", now)) < 7 * 86400
        ][-200:]
        await _merge_agent_writes(state)
        try:
            await state_store.save(state)
        except Exception:
            pass
    return {"state": state, "result": results}


@workflows.activity(start_to_close_timeout=timedelta(minutes=25), retry_policy_max_attempts=2)
async def tasks_check_activity(state: dict[str, Any]) -> dict[str, Any]:
    return await _run_due_tasks(state)


OUTBOX_MAX_PER_RUN = 10
OUTBOX_SENDING_STALE_SECONDS = 600


async def _drain_outbox(state: dict[str, Any]) -> dict[str, Any]:
    import time

    import signal_client
    import state_store

    now = time.time()
    pending = []
    for entry in state.get("outbox", []):
        if entry.get("status") == "pending":
            pending.append(entry)
        elif entry.get("status") == "sending" and now - float(entry.get("started", 0)) > OUTBOX_SENDING_STALE_SECONDS:
            entry["status"] = "pending"
            pending.append(entry)
    results: list[dict[str, Any]] = []
    for entry in pending[:OUTBOX_MAX_PER_RUN]:
        result: dict[str, Any] = {
            "key": f"outbox:{entry.get('id')}",
            "status": "error",
            "chat": entry.get("target", ""),
        }
        entry["status"] = "sending"
        entry["started"] = now
        try:
            await state_store.save(state)
        except Exception:
            pass
        try:
            recipients, chat_key, label = await signal_client.resolve_target(entry.get("target") or "")
            await signal_client.send(recipients, entry.get("message") or "")
            entry["status"] = "done"
            entry["finished"] = now
            entry["chat_key"] = chat_key
            state.setdefault("log", []).append(
                {
                    "ts": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
                    "chat": label,
                    "sender": "JARVIS (proaktiv)",
                    "summary": (entry.get("message") or "")[:240],
                }
            )
            state["log"] = state["log"][-LOG_LIMIT:]
            result.update({"status": "ok", "chat": chat_key})
        except Exception as error:
            entry["status"] = "error"
            entry["finished"] = now
            entry["error"] = str(error)[:300]
            result["error"] = str(error)[:200]
            try:
                owner = _owner_dm_recipient()
                if owner:
                    await signal_client.send(
                        [owner],
                        f"⚠️ Nachricht an {entry.get('target')} konnte nicht gesendet werden: {str(error)[:200]}",
                    )
            except Exception:
                pass
        results.append(result)
    if results:
        state["outbox"] = [
            o
            for o in state.get("outbox", [])
            if o.get("status") in ("pending", "sending") or now - float(o.get("finished", now)) < 7 * 86400
        ][-100:]
        try:
            await state_store.save(state)
        except Exception:
            pass
    return {"state": state, "result": results}


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=2)
async def outbox_check_activity(state: dict[str, Any]) -> dict[str, Any]:
    return await _drain_outbox(state)


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
        # State wird EINMAL pro Run geladen und an die Activities durchgereicht ( jede Activity
        # hat frueher selbst geladen -> 3 HTTP-Calls pro Activity gespart). Jede Activity gibt
        # {"state": ..., "result": ...} zurueck, damit der naechste Schritt den frischen Stand sieht.
        try:
            state = await load_state_activity()
        except Exception:
            state = {"chats": {}, "log": [], "processed": []}
        details: list[dict[str, Any]] = []
        # Agent-Health-Guard VOR dem Signal-Poll: /v1/receive dequeued destructiv — bei "down"
        # wird NICHT gepollt, damit Nachrichten in der Signal-Queue bleiben (kein Verlust).
        try:
            out = await agent_health_activity(state)
            state = out.get("state") or state
            health = out.get("result") or {"status": "ok"}
        except Exception as error:
            health = {"status": "probe-error", "error": str(error)[:200]}
        agent_down = health.get("status") == "down"
        messages: list[dict[str, Any]] = []
        if agent_down:
            details.append(health)
            logger.info("agent down -> Signal-Poll wird uebersprungen (Nachrichten bleiben in der Queue)")
        else:
            if health.get("status") != "ok":
                details.append(health)
            try:
                out = await agent_restart_check_activity(state)
                state = out.get("state") or state
                if out.get("result"):
                    details.append(out["result"])
            except Exception as error:
                details.append({"key": "agent-restart", "status": "error", "error": str(error)[:200]})
            messages = await poll_signal_activity()
            messages = messages[: input.max_messages]
            for message in messages:
                try:
                    out = await process_message_activity(message, state)
                    state = out.get("state") or state
                    details.append(out.get("result") or {"key": message.get("key"), "status": "error"})
                except Exception as error:
                    details.append({"key": message.get("key"), "status": "error", "error": str(error)[:300]})
        if state.get("outbox"):
            try:
                out = await outbox_check_activity(state)
                state = out.get("state") or state
                details.extend(out.get("result") or [])
            except Exception as error:
                details.append({"key": "outbox", "status": "error", "error": str(error)[:300]})
        if not agent_down:
            try:
                out = await mailbox_check_activity(state)
                state = out.get("state") or state
                if out.get("result"):
                    details.append(out["result"])
            except Exception as error:
                details.append({"key": "mailbox", "status": "error", "error": str(error)[:300]})
            if any(task.get("status") in ("pending", "running") for task in state.get("tasks", [])):
                try:
                    out = await tasks_check_activity(state)
                    state = out.get("state") or state
                    details.extend(out.get("result") or [])
                except Exception as error:
                    details.append({"key": "tasks", "status": "error", "error": str(error)[:300]})
        processed = sum(1 for detail in details if detail.get("status") == "ok")
        return BridgeOutput(received=len(messages), processed=processed, details=details)
