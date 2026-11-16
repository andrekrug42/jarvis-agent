import asyncio
import os

import mistralai.workflows as workflows

from bridge import SignalBridgeWorkflow, ensure_agent_registered


async def main() -> None:
    api_key = os.environ.get("JARVIS_AGENT_API_KEY", "").strip()
    if api_key:
        try:
            await ensure_agent_registered(os.environ.get("JARVIS_AGENT_NAME", "jarvis"), api_key)
        except Exception as error:
            print(f"agent registration at startup failed, retried in activity: {error}")
    await workflows.run_worker([SignalBridgeWorkflow])


if __name__ == "__main__":
    asyncio.run(main())
