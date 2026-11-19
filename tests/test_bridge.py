import asyncio

import bridge
import state_store


def test_parse_mail_count():
    assert bridge._parse_mail_count("MAILS: 3 - von X ...") == 3
    assert bridge._parse_mail_count("MAILS: 0 Postfach ist leer") == 0
    assert bridge._parse_mail_count("keine Mails") is None
    assert bridge._parse_mail_count("MAILS: 12") == 12


def test_strip_mail_marker():
    assert bridge._strip_mail_marker("MAILS: 2 - von X kam ...") == "von X kam ..."
    assert bridge._strip_mail_marker("MAILS: 0 Postfach leer") == "Postfach leer"
    assert bridge._strip_mail_marker("MAILS: 1: abc") == "abc"
    assert bridge._strip_mail_marker("normaler Text") == "normaler Text"


def test_truncate():
    assert bridge._truncate("a  b\n c") == "a b c"
    long = "x" * 500
    out = bridge._truncate(long, 240)
    assert len(out) == 240 and out.endswith("…")


def test_is_trusted_image_url():
    assert bridge._is_trusted_image_url("https://files.mistral.ai/img.png")
    assert bridge._is_trusted_image_url("https://mistral.ai/img.png")
    assert not bridge._is_trusted_image_url("https://evil.example.com/img.png")
    assert not bridge._is_trusted_image_url("https://mistral.ai.evil.com/img.png")
    assert not bridge._is_trusted_image_url("http://localhost:8080/x.png")


def _message(**over):
    msg = {
        "key": "k1",
        "timestamp": 1700000000000,
        "chat_key": "dm:uuid-1",
        "chat_label": "1:1-Chat mit Melina Krug",
        "sender_uuid": "uuid-1",
        "sender_number": "+491111",
        "sender_name": "Melina Krug",
        "text": "Hallo Jarvis",
        "attachments": [],
        "recipients": ["uuid-1"],
        "receipt_recipients": ["+491111"],
    }
    msg.update(over)
    return msg


def test_build_envelope_contains_context_and_message():
    env = bridge.build_envelope(
        _message(),
        files_info=[{"filename": "a.png", "content_type": "image/png", "file_id": "f1"}],
        log_lines=["2026-11-18 09:00 | 1:1-Chat mit Andre Krug | Andre Krug: Test -> ok"],
        audio_transcripts=["voice.ogg: Hallo"],
        libraries=[{"id": "lib1", "name": "Dokumente zu Andre"}],
    )
    assert "[KONTEXT" in env
    assert "Chat: 1:1-Chat mit Melina Krug" in env
    assert "Chat-Key: dm:uuid-1" in env
    assert "Absender dieser Nachricht: Melina Krug" in env
    assert "Aktuelle Zeit:" in env and "Europe/Berlin" in env
    assert "Zuletzt in den anderen Chats besprochen:" in env
    assert "file_id=f1" in env
    assert "voice.ogg: Hallo" in env
    assert "Dokumente zu Andre (id: lib1)" in env
    assert env.endswith("[NACHRICHT von Melina Krug]\nHallo Jarvis")


def test_build_envelope_attachment_only_message():
    env = bridge.build_envelope(_message(text=""), files_info=[], log_lines=[])
    assert "(nur Anhänge, kein Text)" in env


def test_merge_agent_writes_keeps_agent_entries_and_local_status(monkeypatch):
    async def fake_load():
        return {
            "chats": {},
            "log": [],
            "processed": [],
            "outbox": [
                {"id": "msg-1", "status": "pending", "target": "Melina", "message": "Hi"},
                {"id": "msg-old", "status": "done", "finished": 1},
            ],
            "tasks": [
                {"id": "task-1", "status": "running"},  # lokal ist er schon done
                {"id": "task-2", "status": "pending"},  # neu vom Agenten
            ],
        }

    monkeypatch.setattr(state_store, "load", fake_load)
    local = {
        "chats": {},
        "log": [],
        "processed": [],
        "outbox": [{"id": "msg-old", "status": "done", "finished": 1}],
        "tasks": [{"id": "task-1", "status": "done", "finished": 2}],
    }
    asyncio.run(bridge._merge_agent_writes(local))
    outbox_ids = {o["id"] for o in local["outbox"]}
    assert outbox_ids == {"msg-1", "msg-old"}
    assert next(o for o in local["outbox"] if o["id"] == "msg-1")["status"] == "pending"
    tasks = {t["id"]: t for t in local["tasks"]}
    assert tasks["task-1"]["status"] == "done"  # lokaler Stand gewinnt
    assert tasks["task-2"]["status"] == "pending"  # neuer Agent-Eintrag übernommen


def test_merge_agent_writes_survives_failed_load(monkeypatch):
    async def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(state_store, "load", boom)
    local = {"outbox": [{"id": "msg-1", "status": "pending"}], "tasks": []}
    asyncio.run(bridge._merge_agent_writes(local))  # darf nicht werfen
    assert local["outbox"] == [{"id": "msg-1", "status": "pending"}]


def test_owner_dm_recipient_env_only(monkeypatch):
    import signal_client

    monkeypatch.delenv("JARVIS_OWNER_UUID", raising=False)
    signal_client._owner_warned = False
    assert bridge._owner_dm_recipient() is None  # kein Default mehr im Code
    monkeypatch.setenv("JARVIS_OWNER_UUID", "uuid-owner")
    assert bridge._owner_dm_recipient() == "uuid-owner"


def _no_load(monkeypatch):
    async def boom():
        raise RuntimeError("state_store.load darf in diesem Pfad nicht mehr gerufen werden")

    monkeypatch.setattr(state_store, "load", boom)


def test_drain_outbox_uses_passed_state_empty(monkeypatch):
    _no_load(monkeypatch)
    state = {"outbox": [], "log": []}
    out = asyncio.run(bridge._drain_outbox(state))
    assert out["result"] == []
    assert out["state"] is state


def test_drain_outbox_sends_pending_without_load(monkeypatch):
    import signal_client

    _no_load(monkeypatch)
    saved = []

    async def fake_save(s):
        saved.append(s)

    monkeypatch.setattr(state_store, "save", fake_save)
    sent = []

    async def fake_send(recipients, message, attachments=None):
        sent.append((recipients, message))
        return {}

    monkeypatch.setattr(signal_client, "send", fake_send)

    async def fake_resolve(target):
        return (["dm:x"], "dm:x", "X")

    monkeypatch.setattr(signal_client, "resolve_target", fake_resolve)
    state = {"outbox": [{"id": "o1", "status": "pending", "target": "Andre", "message": "hi"}], "log": []}
    out = asyncio.run(bridge._drain_outbox(state))
    assert out["result"][0]["status"] == "ok"
    assert sent == [(["dm:x"], "hi")]
    assert out["state"]["outbox"][0]["status"] == "done"
    assert saved  # Status wurde persistiert


def test_run_due_tasks_uses_passed_state_no_tasks(monkeypatch):
    _no_load(monkeypatch)
    out = asyncio.run(bridge._run_due_tasks({"tasks": [], "log": []}))
    assert out["result"] == []


def test_mailbox_check_not_due_uses_passed_state(monkeypatch):
    import time

    _no_load(monkeypatch)
    out = asyncio.run(bridge._mailbox_check({"mailbox_last_check": time.time()}))
    assert out["result"] is None


def test_current_build_sha_caches_within_ttl(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"sha": "abc123"}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    monkeypatch.setattr(bridge, "_build_sha_cache", None)
    monkeypatch.setattr(bridge, "_build_sha_fetched_at", 0.0)
    sha1 = asyncio.run(bridge._current_build_sha())
    sha2 = asyncio.run(bridge._current_build_sha())
    assert sha1 == sha2 == "abc123"
    assert len(calls) == 1  # zweiter Aufruf kam aus dem Cache


def test_current_build_sha_returns_last_known_on_error(monkeypatch):
    class BoomClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            raise RuntimeError("github down")

    monkeypatch.setattr("httpx.AsyncClient", BoomClient)
    monkeypatch.setattr(bridge, "_build_sha_cache", "old-sha")
    monkeypatch.setattr(bridge, "_build_sha_fetched_at", 0.0)
    assert asyncio.run(bridge._current_build_sha()) == "old-sha"


def test_extract_helpers_handle_dataclass_events():
    from dataclasses import dataclass

    @dataclass
    class Entry:
        name: str = "client.generate_image"
        structured_content: object = None

    @dataclass
    class Event:
        entry: object = None

    ev = Event(entry=Entry(structured_content={"image_url": "https://files.mistral.ai/x.png", "file_id": "f1"}))
    assert bridge.extract_image_urls([ev]) == ["https://files.mistral.ai/x.png"]
    assert bridge.extract_file_ids([ev]) == ["f1"]


def test_image_payload_from_event():
    from dataclasses import dataclass

    @dataclass
    class Entry:
        name: str = "client.generate_image"
        structured_content: object = None

    @dataclass
    class Event:
        entry: object = None

    payload = bridge._image_payload_from_event(Event(entry=Entry(structured_content={"image_url": "https://files.mistral.ai/x.png"})))
    assert payload == {"image_url": "https://files.mistral.ai/x.png"}
    # kein Bild im Payload -> None
    assert bridge._image_payload_from_event(Event(entry=Entry(structured_content={"prompt": "katze"}))) is None
    assert bridge._image_payload_from_event(Event(entry=None)) is None
    assert bridge._image_payload_from_event(object()) is None


def test_parse_deployment_health():
    assert bridge._parse_deployment_health({"is_active": True, "workers": [{"id": "w1"}]}, "jarvis") == (True, "is_active=True workers=1")
    ok, err = bridge._parse_deployment_health({"is_active": False, "workers": []}, "jarvis")
    assert ok is False and "is_active=False" in err
    # gewrapptes Shape und workers-allein reichen als "up"
    assert bridge._parse_deployment_health({"deployment": {"is_active": True, "workers": []}}, "jarvis")[0] is True
    assert bridge._parse_deployment_health({"is_active": False, "workers": [{"id": "w"}]}, "jarvis")[0] is True


def test_is_agent_down_error():
    assert bridge._is_agent_down_error(
        Exception(
            "Sessions API answered 409 Conflict to POST /v2/agents/sessions: Agent 'jarvis' cannot start a session "
            "because no worker serves its workflow 'jarvis' on deployment 'jarvis'"
        )
    )
    assert bridge._is_agent_down_error(Exception("409 Conflict"))
    assert not bridge._is_agent_down_error(Exception("httpx.ReadTimeout"))
    assert not bridge._is_agent_down_error(RuntimeError("boom"))


def _reset_agent_probe(monkeypatch):
    monkeypatch.setattr(bridge, "_agent_probe_ok", None)
    monkeypatch.setattr(bridge, "_agent_probe_at", 0.0)
    monkeypatch.setattr(bridge, "_agent_probe_error", "")


def test_agent_health_down_dm_once_then_recovery(monkeypatch):
    import signal_client

    sends = []

    async def fake_send(recipients, message, attachments=None):
        sends.append((recipients, message))

    async def fake_save(state):
        return None

    monkeypatch.setattr(signal_client, "send", fake_send)
    monkeypatch.setattr(state_store, "save", fake_save)
    monkeypatch.setattr(bridge, "_owner_dm_recipient", lambda: "owner-uuid")
    _reset_agent_probe(monkeypatch)

    async def probe_down():
        return (False, "deployment jarvis: ready_replicas=0 phase=failed")

    monkeypatch.setattr(bridge, "_probe_agent", probe_down)
    state = {}
    state, result = asyncio.run(bridge._agent_health(state))
    assert result["status"] == "down" and result["key"] == "agent"
    assert state["agent"]["down"] is True and state["agent"]["down_notified"] is True
    assert len(sends) == 1 and sends[0][0] == ["owner-uuid"] and "nicht erreichbar" in sends[0][1]
    # zweiter Lauf nutzt den Probe-Cache -> kein erneuter DM
    state, result = asyncio.run(bridge._agent_health(state))
    assert result["status"] == "down" and len(sends) == 1

    # Recovery
    async def probe_ok():
        return (True, "ready_replicas=1")

    monkeypatch.setattr(bridge, "_probe_agent", probe_ok)
    monkeypatch.setattr(bridge, "_agent_probe_ok", None)
    monkeypatch.setattr(bridge, "_agent_probe_at", 0.0)
    state, result = asyncio.run(bridge._agent_health(state))
    assert result["status"] == "ok"
    assert state["agent"]["down"] is False
    assert "down_notified" not in state["agent"]
    assert len(sends) == 2 and "wieder erreichbar" in sends[1][1]


def test_agent_health_down_dm_suppressed_during_planned_restart(monkeypatch):
    import time

    import signal_client

    sends = []

    async def fake_send(recipients, message, attachments=None):
        sends.append(message)

    async def fake_save(state):
        return None

    monkeypatch.setattr(signal_client, "send", fake_send)
    monkeypatch.setattr(state_store, "save", fake_save)
    monkeypatch.setattr(bridge, "_owner_dm_recipient", lambda: "owner-uuid")
    _reset_agent_probe(monkeypatch)

    async def probe_down():
        return (False, "ready_replicas=0 phase=failed")

    monkeypatch.setattr(bridge, "_probe_agent", probe_down)
    state = {"agent": {"planned_restart_until": time.time() + 300}}
    state, result = asyncio.run(bridge._agent_health(state))
    assert result["status"] == "down"
    assert sends == []  # geplanter Restart -> kein Owner-DM
    assert state["agent"]["down"] is True


def test_poll_signal_swallows_error_and_notifies_on_timeout(monkeypatch):
    import signal_client

    async def boom_timeout():
        raise RuntimeError("httpx.ReadTimeout: ReadTimeout")

    monkeypatch.setattr(signal_client, "receive", boom_timeout)
    notes = []

    async def fake_notify():
        notes.append(1)

    monkeypatch.setattr(bridge, "_notify_owner_poll_timeout", fake_notify)
    assert asyncio.run(bridge._poll_signal()) == []
    assert len(notes) == 1

    async def boom_other():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(signal_client, "receive", boom_other)
    assert asyncio.run(bridge._poll_signal()) == []
    assert len(notes) == 1  # kein Timeout -> keine Owner-DM


def test_agent_restart_check(monkeypatch):
    calls = []

    async def fake_restart():
        calls.append(1)
        return True, "restarted"

    async def fake_save(state):
        return None

    monkeypatch.setattr(bridge, "_restart_agent_deployment", fake_restart)
    monkeypatch.setattr(state_store, "save", fake_save)
    monkeypatch.setenv("JARVIS_AGENT_RESTART_INTERVAL_SECONDS", "0")
    state, result = asyncio.run(bridge._agent_restart_check({}))
    assert result is None and calls == []

    monkeypatch.setenv("JARVIS_AGENT_RESTART_INTERVAL_SECONDS", "14400")
    state = {}
    state, result = asyncio.run(bridge._agent_restart_check(state))
    assert result is None and calls == [] and state["agent_restart_last"] > 0  # erster Lauf initialisiert nur

    state["agent_restart_last"] -= 20000  # fällig
    state, result = asyncio.run(bridge._agent_restart_check(state))
    assert result["status"] == "ok" and result["key"] == "agent-restart" and len(calls) == 1
    assert state["agent"]["planned_restart_until"] > state["agent_restart_last"]


def test_process_message_guarded_agent_down_dm_and_no_reraise(monkeypatch):
    import signal_client

    async def fake_process_down(message, state):
        raise Exception(
            "Sessions API answered 409 Conflict to POST /v2/agents/sessions: Agent 'jarvis' cannot start a session "
            "because no worker serves its workflow 'jarvis' on deployment 'jarvis'"
        )

    monkeypatch.setattr(bridge, "_process_message", fake_process_down)
    sends = []

    async def fake_send(recipients, message, attachments=None):
        sends.append(message)

    monkeypatch.setattr(signal_client, "send", fake_send)
    monkeypatch.setattr(bridge, "_owner_dm_recipient", lambda: "owner-uuid")
    monkeypatch.setattr(bridge, "_agent_probe_ok", True)
    msg = _message(text="Hey")
    detail, state = asyncio.run(bridge._process_message_guarded(msg, {}))
    assert detail["status"] == "agent-down" and detail["key"] == "k1"
    assert state["agent"]["down"] is True
    assert bridge._agent_probe_ok is False
    assert len(sends) == 1 and "Hey" in sends[0] and "Melina Krug" in sends[0]

    async def fake_process_other(message, state):
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge, "_process_message", fake_process_other)
    raised = False
    try:
        asyncio.run(bridge._process_message_guarded(msg, {}))
    except RuntimeError:
        raised = True
    assert raised
