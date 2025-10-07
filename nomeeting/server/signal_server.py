import websockets
import asyncio
import aiortc
import json
import re
import logging

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 信令服务器
class SignalServer:
    def __init__(self, room, media, host="localhost", port=3000):
        self.host = host
        self.port = port
        self.clients = set()
        self.room = room
        self.media = media

    # 连接处理
    async def handle_client(self, websocket):
        self.clients.add(websocket)
        try:
            async for message in websocket:
                # 处理客户端信令
                msg = json.loads(message)
                typ = msg["type"]
                match typ:
                    case "connect":
                        # 客户端连接
                        rooms = self.room.get_rooms()
                        connect_ack = json.dumps({ "type":"connect_ack", "rooms":f"{rooms}" })
                        await websocket.send(connect_ack)
                    case "join":
                        # 客户加入房间
                        pass
                    case "left":
                        # 客户离开房间
                        pass
                    case "new_room":
                        # 添加房间
                        room_name= msg["room_name"]
                        result = self.room.new_room(room_name)
                        if result == "success":
                            rooms = self.room.get_rooms()
                            ack = json.dumps({ "type":"new_room_success", "rooms": rooms })
                        else:
                            ack = json.dumps({ "type": "new_room_failed", "msg": result })
                        await websocket.send(ack)
                    case "ice":
                        # 客户端ICE候选
                        candidate = msg["candidate"]
                        self.media.ice(candidate)
                    case "offer":
                        # 客户段媒体协商
                        sdp = msg["sdp"]
                        answer = self.media.offer(sdp)
                        ack = json.dumps({ "type": "offer", "answer": ack })
                        await websocket.send(answer)
                    case "stream":
                        # 客户端请求流
                        pass
                    case _:
                        logging.info(f"非定义信令： {message}")
                
        except websockets.ConnectionClosed:
            logging.info(f"连接已关闭： {websocket}")
            self.clients.remove(websocket)

    # 启动服务器
    async def start(self):
        logging.info(f"启动websocket服务器： ws://{self.host}:{self.port}")
        async with websockets.serve(self.handle_client, self.host, self.port):
            await asyncio.Future()