import asyncio

import ui_manager
import media_manager
import signal_client

async def main():
    ui = ui_manager.UiManager()
    media = media_manager.MediaManager(ui)
    signal = signal_client.SignalClient(ui, media)
    task = asyncio.create_task(signal.start())
    await asyncio.gather(task)

if __name__ == "__main__":
    asyncio.run(main())