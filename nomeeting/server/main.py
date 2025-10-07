import asyncio

import signal_server
import room_manager
import media_manager

async def main():
    room = room_manager.RoomManager()
    media = media_manager.MediaManager(room)
    signal = signal_server.SignalServer(room, media, host="localhost", port=3001)
    task = asyncio.create_task(signal.start())
    await asyncio.gather(task)

if __name__ == "__main__":
    asyncio.run(main())