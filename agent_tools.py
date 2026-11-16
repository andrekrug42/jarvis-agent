import asyncio
import base64

from mistralai import agents

MAX_DOCUMENT_CHARS = 120_000


def _mistral_client():
    from mistralai.workflows.client import get_mistral_client
    from mistralai.workflows.core.auth import RunAs

    return get_mistral_client(run_as=RunAs.DEPLOYMENT)


def _download_file(client, file_id: str) -> bytes:
    response = client.files.download(file_id=file_id)
    if hasattr(response, "read"):
        return response.read()
    return bytes(response)


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _ocr_document(data: bytes) -> tuple[str, str]:
    if data.startswith(b"%PDF"):
        return "document_url", f"data:application/pdf;base64,{base64.b64encode(data).decode()}"
    if data.startswith((b"\x89PNG", b"\xff\xd8", b"GIF8")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        return "image_url", f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode()}"
    return "document_url", f"data:application/octet-stream;base64,{base64.b64encode(data).decode()}"


@agents.tool
async def describe_image(file_id: str, question: str = "Beschreibe das Bild ausführlich: Was ist zu sehen, welche Texte/Elemente gibt es?") -> str:
    """Versteht ein Bild (Foto, Screenshot, Grafik, Meme): liest Texte auf dem Bild und beschreibt den Inhalt.

    `file_id`: die file_id des Bild-Anhangs aus dem KONTEXT-Block der Nachricht.
    `question`: die konkrete Frage zum Bild, z. B. "Was steht auf dem Whiteboard?".
    """
    def _run() -> str:
        client = _mistral_client()
        data = _download_file(client, file_id)
        data_url = f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode()}"
        response = client.chat.complete(
            model="mistral-large-4",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else str(content)

    return await asyncio.to_thread(_run)


@agents.tool
async def read_document(file_id: str, question: str = "") -> str:
    """Liest ein PDF oder Dokument per OCR und gibt den Inhalt als Markdown zurück.

    `file_id`: die file_id des Dokument-Anhangs aus dem KONTEXT-Block der Nachricht.
    `question`: optional eine konkrete Frage zum Dokument; dann wird das Dokument gelesen und die Frage direkt beantwortet.
    """
    def _download() -> bytes:
        return _download_file(_mistral_client(), file_id)

    data = await asyncio.to_thread(_download)

    def _ocr() -> str:
        client = _mistral_client()
        doc_type, data_url = _ocr_document(data)
        response = client.ocr.process(
            model="mistral-ocr-latest",
            document={"type": doc_type, doc_type: data_url},
        )
        pages = [getattr(page, "markdown", "") or "" for page in response.pages]
        text = "\n\n".join(pages).strip()
        if len(text) > MAX_DOCUMENT_CHARS:
            text = text[:MAX_DOCUMENT_CHARS] + "\n\n[... Dokument gekürzt ...]"
        return text

    text = await asyncio.to_thread(_ocr)

    if question:
        def _qa() -> str:
            client = _mistral_client()
            response = client.chat.complete(
                model="mistral-large-4",
                messages=[
                    {
                        "role": "system",
                        "content": "Beantworte die Frage ausschließlich anhand des folgenden Dokuments. Antworte auf Deutsch, prägnant.",
                    },
                    {"role": "user", "content": f"Dokument:\n\n{text}\n\nFrage: {question}"},
                ],
            )
            content = response.choices[0].message.content
            return content if isinstance(content, str) else str(content)

        return await asyncio.to_thread(_qa)

    return text or "(Das Dokument enthielt keinen erkennbaren Text.)"
