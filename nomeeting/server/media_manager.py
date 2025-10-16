import aiortc
import os
from aiortc import RTCConfiguration, RTCIceServer
from dataclasses import dataclass
import json
import asyncio
import logging
from aiortc.contrib.media import MediaRelay

# 日志
logging.basicConfig(
    level=logging.INFO,
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
        default_ices = [
            "stun:stun.l.google.com:19302",
            "stun:stun1.l.google.com:19302",
            "stun:stun2.l.google.com:19302",
            "stun:stun3.l.google.com:19302",
            "stun:stun4.l.google.com:19302",
            "stun:stun.voipbuster.com:3478",
            "stun:stun.ekiga.net:3478",
        ]
        urls = default_ices

        # 存储为 RTCIceServer 对象列表，便于创建 RTCConfiguration
        self.ice_servers = [RTCIceServer(urls=[u]) for u in urls]
        # 用于转发单一源 track 到多个 PeerConnection
        self.relay = MediaRelay()

    def register(self, client_id: str, websocket: any):
        """
        注册用户
        
        pc回调在这里注册
        """
        # 使用预先构造的 ice server 列表创建 RTCConfiguration
        try:
            config = RTCConfiguration(iceServers=self.ice_servers)
            pc = aiortc.RTCPeerConnection(configuration=config)
        except Exception:
            # 某些 aiortc 版本可能接受 dict 风格参数，回退到默认构造
            logging.exception("创建 RTCPeerConnection 时使用 RTCConfiguration 失败，回退到默认构造")
            pc = aiortc.RTCPeerConnection()
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
            try:
                if candidate is None:
                    # end of candidates
                    cand_obj = {"candidate": "", "sdpMid": None, "sdpMLineIndex": None}
                else:
                    cand_obj = {
                        "candidate": getattr(candidate, "candidate", None),
                        "sdpMid": getattr(candidate, "sdpMid", None),
                        "sdpMLineIndex": getattr(candidate, "sdpMLineIndex", None),
                    }
                candidate_json = json.dumps(cand_obj)
                ice_msg = json.dumps({ "type": "ice", "client_id": client_id, "candidate": candidate_json })
                _task = asyncio.create_task(websocket.send(ice_msg))
            except Exception:
                logging.exception("发送 ICE candidate 失败")

        @pc.on("track")
        def on_track(track: any):
            """
            核心
            
            流处理
            """
            current_client = self.clients.get(client_id)
            if not current_client:
                return
            clients: dict = self.room.get_neighbors(client_id)
            if clients:
                # 使用 MediaRelay 为每个邻居创建独立订阅的 track
                for c_id, client in clients.items():
                    # 跳过自己
                    if c_id == client_id:
                        continue
                    try:
                        rel_track = self.relay.subscribe(track)
                        client.pc.addTrack(rel_track)
                    except Exception:
                        logging.exception("为邻居添加 track 失败")

    async def offer(self, client_id: str, sdp: any) -> str:
        """
        处理客户端offer
        """
        c = self.clients.get(client_id)
        if c is None:
            logging.warning(f"offer: 未找到 client_id={client_id}")
            raise ValueError("client 未注册")

        offer = aiortc.RTCSessionDescription(sdp=sdp, type="offer")
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
        if c is None:
            logging.warning(f"ice: 未找到 client_id={client_id}")
            return

        # 解析 candidate 字符串并构造 RTCIceCandidate 对象
        try:
            cobj = json.loads(candidate_json)
        except Exception:
            logging.exception("无法解析 candidate_json")
            return

        try:
            # 如果 candidate 字段为空（结束信令），传入 None
            if not cobj.get("candidate"):
                await c.pc.addIceCandidate(None)
                return

            candidate = aiortc.RTCIceCandidate(
                candidate=cobj.get("candidate"),
                sdpMid=cobj.get("sdpMid"),
                sdpMLineIndex=cobj.get("sdpMLineIndex"),
            )
            await c.pc.addIceCandidate(candidate)
        except Exception:
            logging.exception("将 ICE candidate 添加到 pc 时出错")

    async def set_answer(self, client_id: str, sdp: str):
        """为之前 server 发起 offer 的 peer 设置远端 answer"""
        c = self.clients.get(client_id)
        if c is None:
            logging.warning(f"set_answer: 未找到 client_id={client_id}")
            return
        try:
            answer = aiortc.RTCSessionDescription(sdp=sdp, type="answer")
            await c.pc.setRemoteDescription(answer)
            logging.info(f"已为 client {client_id} 设置远端 answer")
        except Exception:
            logging.exception("设置远端 answer 失败")

    def get_client_by_id(self, client_id: str):
        """
        id反查客户端对象
        """
        if client_id in self.clients:
            return self.clients[client_id]