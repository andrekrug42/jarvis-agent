import asyncio
import json

import pytest

import state_store


def test_parse_state_valid_fence():
    text = "```json\n" + json.dumps({"chats": {}, "log": [{"a": 1}], "processed": []}) + "\n```"
    state = state_store._parse_state(text)
    assert state["log"] == [{"a": 1}]
    assert state["chats"] == {}


def test_parse_state_adds_default_keys():
    state = state_store._parse_state(json.dumps({"processed": ["x"]}))
    assert state["chats"] == {}
    assert state["log"] == []
    assert state["processed"] == ["x"]


def test_parse_state_invalid_json_raises():
    with pytest.raises(Exception):
        state_store._parse_state("```json\n{not json}\n```")


def test_parse_state_non_dict_raises():
    with pytest.raises(ValueError):
        state_store._parse_state("```json\n[1,2,3]\n```")


def test_default_state_keys():
    state = state_store._default_state()
    assert state == {"chats": {}, "log": [], "processed": []}


def test_load_falls_back_to_last_good(monkeypatch):
    good = {"chats": {}, "log": [], "processed": ["k1"]}
    monkeypatch.setattr(state_store, "_last_good_state", good)

    def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(state_store, "_load_sync", boom)
    assert asyncio.run(state_store.load()) == good


def test_load_raises_without_last_good(monkeypatch):
    monkeypatch.setattr(state_store, "_last_good_state", None)

    def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(state_store, "_load_sync", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(state_store.load())
