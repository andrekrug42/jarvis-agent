import base64

import signal_client


def test_name_matches_exact_prefix_token():
    assert signal_client._name_matches("Melina", "melina")
    assert signal_client._name_matches("Melina Krug", "melina")
    assert signal_client._name_matches("Krug, Melina", "melina")
    assert signal_client._name_matches("melina krug", "melina krug")
    assert not signal_client._name_matches("Melina", "jano")
    assert not signal_client._name_matches("", "melina")
    assert not signal_client._name_matches("Melina", "")


def test_allowlist_dm_and_group():
    al = signal_client._Allowlist(False, {"uuid-1", "+491111"}, {"gid-1"}, {"die kruhsohs"})
    assert al.allows(sender_uuid="uuid-1", sender_number="", group_id="", group_name="")
    assert al.allows(sender_uuid="", sender_number="+491111", group_id="", group_name="")
    assert not al.allows(sender_uuid="uuid-9", sender_number="", group_id="", group_name="")
    assert al.allows(sender_uuid="", sender_number="", group_id="gid-1", group_name="")
    assert al.allows(sender_uuid="", sender_number="", group_id="gid-unbekannt", group_name="Die Kruhsohs")
    assert not al.allows(sender_uuid="", sender_number="", group_id="gid-9", group_name="andere")
    # DM ohne group_id: Gruppenname zaehlt nicht
    assert not al.allows(sender_uuid="", sender_number="", group_id="", group_name="Die Kruhsohs")


def test_allowlist_fail_closed_when_empty():
    al = signal_client._Allowlist(False, set(), set(), set())
    assert not al.allows(sender_uuid="uuid-1", sender_number="+491111", group_id="", group_name="")
    assert not al.allows(sender_uuid="", sender_number="", group_id="gid-1", group_name="gruppe")


def _dm_envelope(**over):
    env = {
        "timestamp": 1700000000000,
        "sourceUuid": "uuid-1",
        "sourceNumber": "+491111",
        "sourceName": "Melina Krug",
        "dataMessage": {"timestamp": 1700000000000, "message": "Hallo", "attachments": []},
    }
    env.update(over)
    return env


def test_normalize_dm_allowed():
    al = signal_client._Allowlist(False, {"uuid-1"}, set(), set())
    msg = signal_client.normalize(_dm_envelope(), al)
    assert msg is not None
    assert msg["chat_key"] == "dm:uuid-1"
    assert msg["text"] == "Hallo"
    assert msg["recipients"] == ["uuid-1"]
    assert msg["key"] == "1700000000000:uuid-1:1700000000000"


def test_normalize_dm_not_allowed_dropped():
    al = signal_client._Allowlist(False, {"uuid-other"}, set(), set())
    assert signal_client.normalize(_dm_envelope(), al) is None


def test_normalize_own_message_dropped():
    al = signal_client._Allowlist(False, {"uuid-1"}, set(), set())
    env = _dm_envelope(sourceNumber="+4900000000000")  # eigene Nummer aus conftest
    assert signal_client.normalize(env, al) is None


def test_normalize_receipt_without_datamessage_dropped():
    al = signal_client._Allowlist(False, {"uuid-1"}, set(), set())
    assert signal_client.normalize({"envelope": {"receiptMessage": {}}}, al) is None


def test_normalize_group():
    al = signal_client._Allowlist(False, set(), {"gid-1"}, {"die kruhsohs"})
    env = _dm_envelope()
    env["dataMessage"]["groupInfo"] = {"groupId": "gid-1", "name": "Die Kruhsohs"}
    msg = signal_client.normalize(env, al)
    assert msg is not None
    assert msg["chat_key"] == "group:gid-1"
    assert msg["recipients"][0].startswith("group.")
    expected = "group." + base64.b64encode(b"gid-1").decode()
    assert msg["recipients"] == [expected]


def test_normalize_group_not_allowed_dropped():
    al = signal_client._Allowlist(False, set(), {"gid-other"}, set())
    env = _dm_envelope()
    env["dataMessage"]["groupInfo"] = {"groupId": "gid-1", "name": "Die Kruhsohs"}
    assert signal_client.normalize(env, al) is None


def test_normalize_attachments_filtered_without_id():
    al = signal_client._Allowlist(False, {"uuid-1"}, set(), set())
    env = _dm_envelope()
    env["dataMessage"]["attachments"] = [
        {"id": "att-1", "contentType": "image/png", "filename": "a.png"},
        {"contentType": "image/png"},  # ohne id -> verworfen
    ]
    msg = signal_client.normalize(env, al)
    assert [a["id"] for a in msg["attachments"]] == ["att-1"]


def test_basic_token_roundtrip():
    user_b64 = base64.b64encode(b"user").decode()
    pass_b64 = base64.b64encode(b"pass").decode()
    token = signal_client._basic_token(user_b64, pass_b64)
    assert base64.b64decode(token).decode() == "user:pass"


def test_b64decode_passthrough_on_invalid():
    assert signal_client._b64decode("not-base64!!!") == "not-base64!!!"
    assert signal_client._b64decode(base64.b64encode(b"hi").decode()) == "hi"
