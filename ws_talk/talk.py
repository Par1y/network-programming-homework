import sys
import websockets
import asyncio
import logging

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class Talk:
    """
    ws聊天
    """
    def __init__(self, host="localhost", port=5500):
        self.host = host
        self.port = port
        self.go = True
        self.is_connected = False

    async def handle_peer(self, websocket: websockets):
        """
        ws连接
        """
        try:
            async for message in websocket:
                print("\n" + str(websocket.id) + "  " + message + "\nwhat can i say: ", end="", flush=True)
        except Exception as e:
            logging.error(f"{e}")
        finally:
            await websocket.close()

    async def listen(self):
        """
        run server
        """
        logging.info(f"接收监听： ws://{self.host}:{self.port}")
        async with websockets.serve(self.handle_peer, self.host, self.port):
            await asyncio.Future()

    async def conn(self):
        """
        run client
        """
        try:
            addr = await asyncio.to_thread(input, "对方地址(ws://host:port)： ")

            async with websockets.connect(addr) as websocket:
                self.is_connected = True
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader()
                await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
                while self.go:
                    if self.is_connected:
                        print("\nwhat can i say: ", end="", flush=True)
                        line = await reader.readline()
                        if not line:
                            break
                        message = line.decode().strip()
                        await websocket.send(message)
        except Exception as e:
            logging.error(f"{e}")


async def main():
    talk = Talk("0.0.0.0", port=3000)
    listen = asyncio.create_task(talk.listen())
    conn = asyncio.create_task(talk.conn())
    await asyncio.gather(listen, conn)

if __name__ == "__main__":
    asyncio.run(main())