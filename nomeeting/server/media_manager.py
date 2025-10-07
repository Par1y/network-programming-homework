import aiortc
from dataclasses import dataclass
import json
import asyncio

@dataclass
class client:
    nickname: str
    ws: any
    pc: aiortc.RTCPeerConnection

class MediaManager:
    def __init__(self, room):
        self.room = room
        self.clients = {}
        self.ice_servers = [{"urls": "stun:stun.nextcloud.com:443"}]  # 公共STUN服务器

    # 用户注册
    def register(self, client_id, websocket):
        pc = aiortc.RTCPeerConnection(self.ice_servers)
        self.clients[client_id] = client("", websocket, pc)
        
        # 断线处理
        @pc.on("connectionstatechange")
        def on_connection_state_change(self):
            if pc.connectionState == "failed":
                pc.close()

        # 准备好了发送自己的ice候选
        @pc.on("icecandidate")
        def on_ice_candidate(candidate):
            candidate_json = json.dumps(candidate)
            ice_msg = json.dumps({ "type": "ice", "client_id": client_id, "candidate": candidate_json })
            _task = asyncio.create_task(websocket.send(ice_msg))

        # 核心流处理
        @pc.on("track")
        def on_track(self, track, receiver):
            pass

    # 处理客户端offer
    async def offer(self, client_id, sdp) -> str:
        c = self.clients.get(client_id)
        offer = aiortc.rtcsessiondescription(sdp=sdp, type="offer")
        await c.pc.setRemoteDescription(offer)

        # 生成SDP Answer
        answer = await c.pc.createAnswer()
        await c.pc.setLocalDescription(answer)
        return answer.sdp

    # 处理客户端ICE候选
    async def ice(self, client_id, candidate_json):
        c = self.clients.get(client_id)
        candidate = json.loads(candidate_json, object_hook=aiortc.RTCIceCandidate)
        await c.pc.addIceCandidate(candidate)