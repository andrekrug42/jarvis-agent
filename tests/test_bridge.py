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


def test_owner_dm_recipient_default():
    import importlib

    import signal_client

    importlib.reload(signal_client)
    assert bridge._owner_dm_recipient() == signal_client.DEFAULT_OWNER_UUID
