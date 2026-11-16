import asyncio
import os

import mistralai.workflows as workflows

from bridge import SignalBridgeWorkflow, ensure_agent_registered


async def main() -> None:
    api_key = os.environ.get("JARVIS_AGENT_API_KEY", "").strip()
    agent_name = os.environ.get("JARVIS_AGENT_NAME", "jarvis")
    agent_task: asyncio.Task[None] | None = None
    if api_key:
        try:
            deployment_name = await ensure_agent_registered(agent_name, api_key)
            if deployment_name and deployment_name != os.environ.get("DEPLOYMENT_NAME", "").strip():
                from main import agent

                agent_task = asyncio.create_task(agent.run_worker(workflow_name=agent_name, deployment_name=deployment_name))
        except Exception as error:
            print(f"agent registration at startup failed, retried in activity: {error}")
    worker = workflows.run_worker([SignalBridgeWorkflow])
    if agent_task is not None:
        await asyncio.gather(worker, agent_task)
    else:
        await worker


if __name__ == "__main__":
    asyncio.run(main())
