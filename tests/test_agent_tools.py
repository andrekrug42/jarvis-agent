import pytest

agent_tools = pytest.importorskip("agent_tools")


def test_image_mime_magic_bytes():
    assert agent_tools._image_mime(b"\x89PNG\r\n") == "image/png"
    assert agent_tools._image_mime(b"\xff\xd8\xff") == "image/jpeg"
    assert agent_tools._image_mime(b"GIF89a") == "image/gif"
    assert agent_tools._image_mime(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
    assert agent_tools._image_mime(b"other") == "image/jpeg"


def test_ocr_document_pdf_and_image():
    kind, url = agent_tools._ocr_document(b"%PDF-1.4 rest")
    assert kind == "document_url" and url.startswith("data:application/pdf;base64,")
    kind, url = agent_tools._ocr_document(b"\x89PNG\r\n rest")
    assert kind == "image_url" and url.startswith("data:image/png;base64,")


def test_conversation_text_collects_text_and_urls():
    response = type(
        "R",
        (),
        {
            "outputs": [
                {
                    "type": "message.output",
                    "content": [
                        {"type": "text", "text": "Antwort"},
                        {"type": "text", "text": "mit Quelle", "annotations": [{"url_citation": {"url": "https://example.com/q"}}]},
                    ],
                }
            ]
        },
    )()
    text, urls = agent_tools._conversation_text(response)
    assert "Antwort" in text and "mit Quelle" in text
    assert urls == ["https://example.com/q"]


def test_model_env_defaults():
    import importlib

    importlib.reload(agent_tools)
    assert agent_tools.SEARCH_MODEL == "mistral-medium-latest"
    assert agent_tools.IMAGE_MODEL == "mistral-large-4"
    assert agent_tools.VISION_MODEL == "mistral-large-4"
    assert agent_tools.TASK_PARSE_MODEL == "mistral-small-latest"
