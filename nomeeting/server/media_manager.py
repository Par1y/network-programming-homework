import aiortc
from dataclasses import dataclass
import json
import asyncio
import logging

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

@dataclass
class Client:
    """
    客户端类

    `nickname: str` 客户端昵称

    `ws: any` 客户端websocket

    `pc: aiortc.RTCPeerConnection` 客户端RTC连接
    """
    nickname: str
    ws: any
    pc: aiortc.RTCPeerConnection

class MediaManager:
    """
    媒体管理器

    维护`clients` { client_id: Client }

    处理WebRTC所有功能
    """
    def __init__(self, room):
        self.room = room
        self.clients = {}
        self.ice_servers = [{"urls": "stun:stun.nextcloud.com:443"}]  # 公共STUN服务器

    def register(self, client_id: str, websocket: any):
        """
        注册用户
        
        pc回调在这里注册
        """
        pc = aiortc.RTCPeerConnection(self.ice_servers)
        self.clients[client_id] = Client("", websocket, pc)
        
        @pc.on("iceconnectionstatechange")
        def on_ice_connection_change():
            if pc.iceConnectionState == "connected":
                logging.info("ICE已连接。")
            elif pc.iceConnectionState == "failed":
                logging.warning("ICE连接失败。")

        @pc.on("connectionstatechange")
        def on_connection_state_change():
            """
            断线处理
            """
            if pc.connectionState == "failed":
                _ack = self.room.left(client_id)
                pc.close()

        @pc.on("icecandidate")
        def on_ice_candidate(candidate: any):
            """
            准备好了发送自己的ice候选
            """
            candidate_json = json.dumps(candidate)
            ice_msg = json.dumps({ "type": "ice", "client_id": client_id, "candidate": candidate_json })
            _task = asyncio.create_task(websocket.send(ice_msg))

        @pc.on("track")
        def on_track(track: any, receiver: any):
            """
            核心
            
            流处理
            """
            current_client = self.clients.get(client_id)
            if not current_client:
                return
            clients: dict = self.room.get_neighbors(client_id)
            if clients:
                for c_id, client in clients:
                    if c_id == current_client:
                        continue # 跳过自己
                    client.pc.addTrack(track)

    async def offer(self, client_id: str, sdp: any) -> str:
        """
        处理客户端offer
        """
        c = self.clients.get(client_id)
        offer = aiortc.rtcsessiondescription(sdp=sdp, type="offer")
        await c.pc.setRemoteDescription(offer)

        # 生成SDP Answer
        answer = await c.pc.createAnswer()
        await c.pc.setLocalDescription(answer)
        return answer.sdp

    async def ice(self, client_id: str, candidate_json: str):
        """
        处理客户端ICE候选
        """
        c = self.clients.get(client_id)
        candidate = json.loads(candidate_json, object_hook=aiortc.RTCIceCandidate)
        await c.pc.addIceCandidate(candidate)

    def get_client_by_id(self, client_id: str):
        """
        id反查客户端对象
        """
        if client_id in self.clients:
            return self.clients[client_id]