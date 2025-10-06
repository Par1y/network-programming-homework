import asyncio

import signal_server

async def main():
    signal = signal_server.SignalServer(host="localhost", port=3001)
    task = asyncio.create_task(signal.start())
    await asyncio.gather(task)

if __name__ == "__main__":
    asyncio.run(main())