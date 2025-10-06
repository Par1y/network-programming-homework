import websockets
import asyncio
import aiortc
import logging

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 信令服务器
class SignalServer:
    def __init__(self, host="localhost", port=3000):
        self.host = host
        self.port = port
        self.clients = set()

    # 连接处理
    async def handle_client(self, websocket):
        try:
            async for message in websocket:
                # 处理客户端信令
                typ = message["type"]
                match typ:
                    case "join":
                        # call room join
                        pass
                    case "left":
                        # call room left
                        pass
                    case "ice":
                        # call media ice
                        pass
                    case "offer":
                        # call media offer
                        pass
                    case "stream":
                        # call media steam
                        pass
                    case _:
                        pass
        except websockets.ConnectionClosed:
            logging.info(f"连接已关闭： {websocket}")
            self.clients.remove(websocket)

    # 启动服务器
    async def start(self):
        logging.info(f"启动websocket服务器： ws://{self.host}:{self.port}")
        async with websockets.serve(self.handle_client, self.host, self.port):
            await asyncio.Future()