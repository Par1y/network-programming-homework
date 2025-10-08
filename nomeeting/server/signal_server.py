import uuid
import websockets
import asyncio
import json
import logging
from room_manager import RoomManager
from media_manager import MediaManager

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class SignalServer:
    """
    信令服务器类
    
    只维护ws连接池

    注入RoomManager、MediaManager
    """
    def __init__(self, room, media, host="localhost", port=3000):
        self.host = host
        self.port = port
        self.clients_ws = set()
        self.room: RoomManager = room
        self.media: MediaManager = media

    async def handle_client(self, websocket):
        """
        信令服务器核心

        处理客户端WS连接回调
        """
        self.clients_ws.add(websocket)
        try:
            async for message in websocket:
                # 处理客户端信令
                msg = json.loads(message)
                typ = msg["type"]
                match typ:
                    case "connect":
                        # 客户端连接
                        client_id = str(uuid.uuid4())
                        self.media.register(client_id, websocket)
                        rooms = self.room.get_rooms()
                        connect_ack = json.dumps({ "type":"connect_ack", "rooms":f"{rooms}", "client_id": client_id })
                        await websocket.send(connect_ack)

                    case "join":
                        # 客户加入房间
                        client_id = msg["client_id"]
                        room_name = msg["room_name"]
                        client = self.media.get_client_by_id(client_id)
                        ack: str = self.room.join(client_id, room_name, client)
                        if ack == "success":
                            ack = json.dumps({ "type": "join_success" })
                        else:
                            ack = json.dumps({ "type": "join_failed", "msg": ack })
                        await websocket.send(ack)

                    case "left":
                        # 客户离开房间
                        client_id = msg["client_id"]
                        room_name = msg["room_name"]
                        ack: str = self.room.left(client_id, room_name)
                        if ack == "success":
                            ack = json.dumps({ "type": "left_success" })
                        else:
                            ack = json.dumps({ "type": "left_failed", "msg": ack })
                        await websocket.send(ack)

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
                        client_id = msg["client_id"]
                        candidate_json = msg["candidate"]
                        await self.media.ice(client_id, candidate_json)

                    case "offer":
                        # 客户段媒体协商
                        client_id = msg["client_id"]
                        sdp = msg["sdp"]
                        answer = await self.media.offer(client_id, sdp)
                        ack = json.dumps({ "type": "offer", "answer": ack })
                        await websocket.send(answer)

                    case "stream":
                        # 客户端请求流
                        pass

                    case _:
                        logging.info(f"非定义信令： {message}")

        except websockets.ConnectionClosed:
            logging.info(f"连接已关闭： {websocket}")
            self.clients_ws.remove(websocket)

    async def start(self):
        """
        启动websocket信令服务器
        """
        logging.info(f"启动websocket服务器： ws://{self.host}:{self.port}")
        async with websockets.serve(self.handle_client, self.host, self.port):
            await asyncio.Future()