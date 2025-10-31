import uuid
import websockets
import asyncio
import json
import logging
from room_manager import RoomManager
from media_manager import MediaManager

# 日志
logging.basicConfig(
    level=logging.INFO,
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
        client_id = None  # 记录当前连接的client_id，用于断线清理
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
                        logging.info(f"客户端 {client_id} 请求加入房间 {room_name}")
                        
                        client = self.media.get_client_by_id(client_id)
                        ack: str = self.room.join(client_id, room_name, client)
                        if ack == "success":
                            logging.info(f"客户端 {client_id} 成功加入房间")
                            
                            # 检查房间内是否有已存在的tracks
                            neighbors = self.room.get_neighbors(client_id)
                            has_existing_tracks = False
                            
                            if neighbors:
                                for neighbor_id in neighbors.keys():
                                    if neighbor_id != client_id:
                                        neighbor_tracks = self.media.client_tracks.get(neighbor_id, [])
                                        if neighbor_tracks:
                                            has_existing_tracks = True
                                            break
                            # 先发送join_success，这样客户端才能在收到服务器offer之前知道服务器会发送offer
                            ack = json.dumps({
                                "type": "join_success",
                                "server_will_offer": has_existing_tracks
                            })
                            await websocket.send(ack)
                            
                            # 此时客户端等待服务器offer，再订阅房间内已有的流
                            await self.media.subscribe_existing_tracks(client_id, room_name, self.room)
                            
                            # 触发 joined_event，允许 on_track 中的转发任务继续执行
                            client.joined_event.set()
                            logging.info(f"客户端 {client_id} 的 joined_event 已触发")
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
                        logging.info(f"收到客户端 {client_id} 的offer")
                        answer = await self.media.offer(client_id, sdp)
                        # 返回 answer 给发起 offer 的客户端
                        ack = json.dumps({ "type": "answer", "sdp": answer })
                        await websocket.send(ack)
                        logging.info(f"已向客户端 {client_id} 发送answer")

                    case "answer":
                        # 客户端对服务器发起的 offer 的 answer
                        client_id = msg.get("client_id")
                        sdp = msg.get("sdp")
                        if client_id and sdp:
                            await self.media.set_answer(client_id, sdp)

                    case _:
                        logging.info(f"非定义信令： {message}")

        except websockets.ConnectionClosed:
            logging.info(f"连接已关闭： {websocket}")
            self.clients_ws.remove(websocket)
            # 清理断线客户端的所有资源
            if client_id:
                await self._cleanup_client(client_id)
        except Exception as e:
            logging.exception(f"处理客户端消息时出错: {e}")
            if client_id:
                await self._cleanup_client(client_id)
        finally:
            # 确保websocket被移除
            self.clients_ws.discard(websocket)
    
    async def _cleanup_client(self, client_id: str):
        """
        委托 MediaManager 和 RoomManager 清理客户端资源。
        """
        logging.critical(f"--- 根本原因诊断 --- [SignalServer] 准备为客户端 {client_id} 调用 remove_client。触发源：WebSocket关闭或异常。")
        logging.info(f"[SignalServer] 委托清理客户端资源: {client_id}")
        try:
            await self.media.remove_client(client_id)

            logging.info(f"[SignalServer] 客户端 {client_id} 的资源委托清理完成。")
        except Exception as e:
            logging.exception(f"[SignalServer] 委托清理客户端 {client_id} 资源时出错: {e}")

    async def start(self):
        """
        启动websocket信令服务器
        """
        logging.info(f"启动websocket服务器： ws://{self.host}:{self.port}")
        async with websockets.serve(self.handle_client, self.host, self.port):
            await asyncio.Future()