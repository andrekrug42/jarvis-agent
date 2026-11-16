import asyncio

import mistralai.workflows as workflows

from bridge import SignalBridgeWorkflow


async def main() -> None:
    await workflows.run_worker([SignalBridgeWorkflow])


if __name__ == "__main__":
    asyncio.run(main())
