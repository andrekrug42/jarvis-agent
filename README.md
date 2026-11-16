# JARVIS – persönlicher KI-Assistent auf Mistral Cloud (mit Signal)

JARVIS ist ein Agent auf Basis des **Mistral Agents SDK** (`mistralai-agents`), der als
**verwaltete Bereitstellung (managed deployment)** auf Mistral Cloud läuft und über eine
**Signal-REST-API** erreichbar ist: 1:1-Chats mit einzelnen Familienmitgliedern und Gruppenchats.

## Architektur

```
Signal-App ──► signal-cli-rest-api ──GET /v1/receive──┐
                                                       ▼
                                     ┌──────────────────────────────┐
                                     │ Managed Deployment "jarvis"  │
                                     │  Workflow "jarvis-signal-    │
                                     │  bridge" (Schedule: 15 s)    │
                                     │   poll → pro Chat eine Agent- │
                                     │   Session → Antwort via       │
                                     │   POST /v2/send               │
                                     │  Agent "jarvis" (Agents SDK) │
                                     │   Tools: describe_image,     │
                                     │   read_document (OCR),       │
                                     │   Connector: image_generation│
                                     └──────────────────────────────┘
```

![Architektur](docs/architecture.svg)

- **Agent `jarvis`** (`main.py`): Modell `mistral-large-4`, Persona und Regeln in den Instructions.
- **Bridge-Workflow `jarvis-signal-bridge`** (`bridge.py`): wird alle 15 s per Schedule ausgeführt,
  pollt `GET /v1/receive/{number}`, verarbeitet jede neue Nachricht und antwortet über `POST /v2/send`.
  Dabei: Schreib-Indikator an beim Verarbeiten (`PUT /v1/typing-indicator/{number}`), aus nach der
  Antwort (`DELETE`), und „gelesen“-Receipt nach der Antwort (`POST /v1/receipts/{number}`, receipt_type=read).
- Pro Signal-Chat (DM = Absender-UUID, Gruppe = groupId) wird **eine Agent-Session** geführt
  (`start_session(idempotency_key="signal-chat:<chat>")`) – dadurch merkt sich JARVIS jedes Gespräch.
  Idempotency-Keys sorgen dafür, dass weder Sessions noch Nachrichten doppelt angelegt werden.
- **Übergreifendes Gedächtnis**: Die Bridge führt ein Kurzprotokoll aller Chats (`log` in
  `state_store`, als JSON in einem Dokument der Mistral-Library `jarvis-bridge-state`) und packt die letzten Einträge jeder
  Nachricht als Kontext mit hinein. So weiß JARVIS, mit wem er schreibt und was mit den anderen
  Personen besprochen wurde.
- **Bilder**: verstehen via `describe_image` (Vision), generieren via den eingebauten
  `image_generation`-Connector (läuft als Deployment, keine Anmeldung nötig). Generierte Bilder
  werden als Anhang zurück in den Signal-Chat geschickt.
- **PDFs/Dokumente**: `read_document` (Mistral OCR, `mistral-ocr-latest`).
- **Sprachnachrichten**: werden von der Bridge per Mistral Voxtral (`/v1/audio/transcriptions`) transkribiert und als Text in den Kontext-Block gepackt.

## Voraussetzungen

1. Eine laufende **Signal-REST-API**, z. B. [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)
   im Docker-Container (`MODE=native` oder `normal`), erreichbar für die Mistral-Cloud
   (z. B. über einen Tunnel/Reverse-Proxy auf deinem Server – nur von Mistral aus erreichbar, nicht öffentlich indexiert).
   Registrierte Nummer z. B. `+49…`.
2. **Vier Workspace-Secrets** in Mistral Studio ([console.mistral.ai → Secrets](https://console.mistral.ai/secrets)):
   - `signal_api_url` – Basis-URL der Signal-REST-API, z. B. `https://signal-api.example.internal:8080`
   - `signal_phone_number` – die registrierte Nummer, z. B. `+49123456789`
   - `signal_username` – Username für Basic-Auth, **base64-kodiert**
   - `signal_password` – Passwort für Basic-Auth, **base64-kodiert**

   Daraus baut die Bridge den Header `Authorization: Basic base64(user:pass)`.
   Werte niemals in den Chat/ ins Repo schreiben – die Bridge liest sie zur Laufzeit aus der Umgebung.
   - `jarvis_agent_api_key` – dein persönlicher Mistral-API-Key (User-Key), an das Deployment
     `jarvis-bridge` als `JARVIS_AGENT_API_KEY` gebunden. Die Bridge braucht ihn, weil Agent-Sessions
     nur als User (nicht als Service Account des Deployments) gestartet werden dürfen (`user_id_required`).
3. Ein **GitHub-Repository**, das der GitHub-Connector (`github_app`) in diesem Workspace lesen darf.

## Deployment (verwaltet)

```bash
# 1. lokal testen (optional)
cp .env.example .env   # MISTRAL_API_KEY eintragen
uv sync
uv run --env-file .env agents info
uv run --env-file .env python -c "import bridge"  # Bridge importieren (Syntax/Discovery)

# 2. Agenten-Deployment anlegen (reines Workflows-Deployment, z. B. via MCP/UI):
#    deploy_workflow_deployment(deployment_name="jarvis", github_url=<repo>, revision=<SHA>,
#    dockerfile_path="Dockerfile") – der Worker pollt die Queue "jarvis".
#    Die Agent-Registrierung (POST /v2/agents/agents/register, Bindung Name → Queue "jarvis")
#    übernimmt die Bridge selbst beim Worker-Start mit JARVIS_AGENT_API_KEY
#    (bridge_worker.py → ensure_agent_registered); `agents deploy` ist nicht nötig.
#    Alternativ geht auch: uv run --env-file .env agents deploy . --name jarvis
```

Die **Bridge** (`jarvis-bridge`) wird dagegen als reines Workflows-Deployment angelegt,
z. B. über die Workflows-API/MCP:

```
deploy_workflow_deployment(
  deployment_name="jarvis-bridge",
  github_url="https://github.com/andrekrug42/jarvis-agent",
  revision=<Commit-SHA>,
  build_directory=".",
  dockerfile_path="Dockerfile.bridge",
)
# Secrets in der Studio-UI binden (Deployment jarvis-bridge → Settings → Secrets):
#   SIGNAL_API_URL ← signal_api_url, SIGNAL_PHONE_NUMBER ← signal_phone_number,
#   SIGNAL_USERNAME ← signal_username, SIGNAL_PASSWORD ← signal_password,
#   JARVIS_AGENT_API_KEY ← jarvis_agent_api_key
# dann Deployment neu starten (stop/start), damit die Env-Variablen injiziert werden
```

Danach den Bridge-Workflow **schedulen** (alle 15 s):

```
workflow_identifier: jarvis-signal-bridge
deployment_name: jarvis-bridge
schedule: { input: {}, intervals: [{ every: "PT15S" }], policy: { overlap: 2 } }
```

## Mit JARVIS sprechen

- **Über Signal**: einfach eine Nachricht an die registrierte Nummer schreiben – 1:1 oder in einer Gruppe.
- **Über Vibe / Mistral**: nach dem Deploy erscheint der Agent in den Managed Agents:
  - Vibe Code Web / Chat: `https://chat.mistral.ai/managed-agents/jarvis?source=agents_api`
  - CLI: `agents chat jarvis` (mit `MISTRAL_API_KEY`)
  - Programmatisch: `agents.Agent.load_from(agent_name="jarvis")` + `start_session()` / `run(...)`
  - In Vibe mit verbundenem Mistral-MCP: `list_workflows` zeigt `jarvis` (Agent) und `jarvis-signal-bridge`;
    mit `execute_workflow` lässt sich der Bridge-Workflow manuell einmal auslösen (Test).
- **Sessions/Traces** prüfen: [console.mistral.ai → Managed Agents → Sessions](https://console.mistral.ai/build/managed-agents/sessions)

## Weitere Tools ergänzen

Ein Tool ist eine Python-Funktion mit `@agents.tool` (typisierte Parameter + Docstring) in
`agent_tools.py`, eingetragen in `Agent(tools=[...])` in `main.py`. Beispiel:

```python
@agents.tool
async def get_weather(city: str) -> dict:
    """Aktuelles Wetter für eine Stadt."""
    ...
```

Danach Commit pushen und erneut deployen (`agents deploy . --name jarvis …` mit denselben
`--secret-binding`s) – die bestehende Bereitstellung wird aktualisiert.

## Umgebungsvariablen

| Variable | Pflicht | Bedeutung |
| --- | --- | --- |
| `SIGNAL_API_URL` | ja | Basis-URL der Signal-REST-API (ohne `/v1`/`/v2`) |
| `SIGNAL_PHONE_NUMBER` | ja | registrierte Signal-Nummer im internationalen Format |
| `SIGNAL_USERNAME` | ja* | Username für Basic-Auth, base64-kodiert (*oder `SIGNAL_API_KEY`) |
| `SIGNAL_PASSWORD` | ja* | Passwort für Basic-Auth, base64-kodiert |
| `SIGNAL_API_KEY` | nein | API-Key der REST-API als Alternative zu Basic-Auth |
| `SIGNAL_AUTH_HEADER` | nein | Header-Name für den Key, Standard `Authorization` |
| `SIGNAL_AUTH_PREFIX` | nein | Präfix des Header-Werts, Standard `Bearer ` |
| `SIGNAL_ALLOWED_SOURCES` | nein | kommaseparierte UUIDs/Nummern, die JARVIS beantworten darf; leer = alle |
| `JARVIS_AGENT_NAME` | nein | Name des Agenten, Standard `jarvis` |
| `JARVIS_AGENT_API_KEY` | ja (Bridge) | User-API-Key für `Agent.load_from`/`start_session` (Sessions nur als User möglich); Fallback: Service-Account-Token des Deployments |
| `JARVIS_OWNER_NAME` | nein | Name des Account-Inhabers für den Kontext |

## Hinweise

- Der Bridge-Workflow läuft als Deployment-Identität (Service Account) – daher
  `run_as="deployment"` beim image_generation-Connector und `RunAs.DEPLOYMENT` für eigene
  Mistral-API-Aufrufe in Tools. Es wird keine On-behalf-of-Anmeldung benötigt, das Deployment
  ist daher nicht gehärtet (hardened).
- Sessions enden spätestens nach 24 h idle bzw. 1 Woche gesamt (Maximum bei verwalteten
  Deployments, gesetzt im Dockerfile). Danach startet die Bridge automatisch eine neue Session;
  das Chat-Protokoll bleibt im Bridge-Log erhalten.
- Antworten können durch das 15-s-Polling bis zu ~15 s dauern.
