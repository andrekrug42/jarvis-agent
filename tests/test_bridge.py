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
