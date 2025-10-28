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

    `ws: any` 客户端websocket

    `pc: aiortc.RTCPeerConnection` 客户端RTC连接
    """
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
        # 保存后台任务引用，防止被垃圾回收
        self._background_tasks = []
        # Track注册表：记录每个客户端的所有tracks
        # 格式: { client_id: [track1, track2, ...] }
        self.client_tracks = {}
        default_ices = [
            "stun:stun.l.google.com:19302",
            "stun:stun1.l.google.com:19302",
            "stun:stun2.l.google.com:19302",
            "stun:stun3.l.google.com:19302",
            "stun:stun4.l.google.com:19302",
            "stun:stun.voipbuster.com:3478",
            "stun:stun.ekiga.net:3478",
            "stun:stun.qq.com:3478",
            "stun:stun.miwifi.com:3478",
            "stun:stun.sipgate.net:3478",
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
        self.clients[client_id] = Client(websocket, pc)
        
        @pc.on("iceconnectionstatechange")
        def on_ice_connection_change():
            if pc.iceConnectionState == "connected":
                logging.info(f"ICE已连接 (client_id={client_id})")
            elif pc.iceConnectionState == "failed":
                logging.warning(f"ICE连接失败 (client_id={client_id})")

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
                    cand_obj = {"candidate": "", "sdpMid": None, "sdpMLineIndex": None}
                else:
                    cand_obj = {
                        "candidate": getattr(candidate, "candidate", None),
                        "sdpMid": getattr(candidate, "sdpMid", None),
                        "sdpMLineIndex": getattr(candidate, "sdpMLineIndex", None),
                    }
                candidate_json = json.dumps(cand_obj)
                ice_msg = json.dumps({ "type": "ice", "client_id": client_id, "candidate": candidate_json })
                
                task = asyncio.create_task(websocket.send(ice_msg))
                self._background_tasks.append(task)
            except Exception:
                logging.exception("发送 ICE candidate 失败")

        @pc.on("track")
        def on_track(track: any):
            """
            核心流处理
            """
            logging.info(f"收到 track (kind={track.kind}, client_id={client_id})")
            
            current_client = self.clients.get(client_id)
            if not current_client:
                logging.warning(f"未找到客户端 (client_id={client_id})")
                return
            
            # 将track注册到track注册表
            if client_id not in self.client_tracks:
                self.client_tracks[client_id] = []
            self.client_tracks[client_id].append(track)
            logging.info(f"客户端 {client_id} 现有 {len(self.client_tracks[client_id])} 个track")
            
            neighbors = self.room.get_neighbors(client_id)
            logging.info(f"客户端 {client_id} 的邻居数量: {len(neighbors) if neighbors else 0}")
            
            if not neighbors:
                logging.warning(f"客户端 {client_id} 没有邻居，track已注册但未转发")
                return
            
            for neighbor_id, neighbor in neighbors.items():
                if neighbor_id == client_id:
                    continue
                
                # 检查邻居连接状态，防止向已关闭的连接添加轨道
                if neighbor.pc.connectionState in ("closed", "failed"):
                    logging.warning(f"邻居 {neighbor_id} 的连接状态为 {neighbor.pc.connectionState}，跳过轨道转发")
                    continue
                
                try:
                    rel_track = self.relay.subscribe(track)
                    # addTrack返回RTCRtpSender，需要通过getTransceivers获取transceiver
                    sender = neighbor.pc.addTrack(rel_track)
                    
                    # 获取刚添加的transceiver（最后一个）
                    transceivers = neighbor.pc.getTransceivers()
                    if transceivers:
                        transceiver = transceivers[-1]
                        # 设置direction为sendonly（服务器向客户端发送）
                        transceiver._direction = "sendonly"
                    
                    logging.info(f"已转发 track 到邻居 {neighbor_id}")
                    
                    # 添加track后需要重新协商
                    async def renegotiate():
                        try:
                            offer = await neighbor.pc.createOffer()
                            await neighbor.pc.setLocalDescription(offer)
                            # 等待ICE gathering完成
                            await asyncio.sleep(1.0)
                            
                            offer_msg = json.dumps({
                                "type": "offer",
                                "client_id": neighbor_id,
                                "sdp": offer.sdp
                            })
                            await neighbor.ws.send(offer_msg)
                            logging.info(f"重新协商完成 (neighbor_id={neighbor_id})")
                        except Exception as e:
                            logging.warning(f"重新协商失败 (neighbor_id={neighbor_id}): {e}")
                    
                    task = asyncio.create_task(renegotiate())
                    self._background_tasks.append(task)
                    
                except Exception:
                    logging.exception(f"转发 track 失败 (neighbor_id={neighbor_id})")

    async def offer(self, client_id: str, sdp: any) -> str:
        """
        处理客户端offer
        """
        c = self.clients.get(client_id)
        if c is None:
            logging.warning(f"offer: 未找到 client_id={client_id}")
            raise ValueError("client 未注册")
        
        # --- 竞争条件修复 ---
        # 如果信令状态不是 stable，说明服务器正在进行协商（例如，服务器刚刚发送了一个offer）
        # 此时应忽略客户端的offer，以避免状态冲突。
        if c.pc.signalingState != "stable":
            logging.warning(f"忽略来自客户端 {client_id} 的offer，因为当前信令状态为 '{c.pc.signalingState}'（非 'stable'）")
            return

        offer = aiortc.RTCSessionDescription(sdp=sdp, type="offer")
        await c.pc.setRemoteDescription(offer)

        # 在创建answer前检查并修复transceivers的direction
        for i, t in enumerate(c.pc.getTransceivers()):
            direction = getattr(t, '_direction', None)
            
            # 如果direction为None，尝试修复
            if direction is None:
                sender = getattr(t, 'sender', None)
                if sender and sender.track:
                    t._direction = "sendonly"
                    logging.debug(f"修复Transceiver[{i}]的direction为sendonly")
                else:
                    t._direction = "recvonly"
                    logging.debug(f"修复Transceiver[{i}]的direction为recvonly")

        # 生成SDP Answer
        try:
            answer = await c.pc.createAnswer()
            await c.pc.setLocalDescription(answer)
            return answer.sdp
        except Exception as e:
            logging.error(f"客户端 {client_id} 创建answer失败: {e}")
            raise

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
            logging.info(f"已设置远端 answer (client_id={client_id})")
        except Exception:
            logging.exception(f"设置远端 answer 失败 (client_id={client_id})")

    def get_client_by_id(self, client_id: str):
        """
        id反查客户端对象
        """
        if client_id in self.clients:
            return self.clients[client_id]
    
    async def remove_client(self, client_id: str):
        """Gracefully removes a client and cleans up all associated resources."""
        logging.info(f"[MediaManager] 开始清理客户端: {client_id}")
        client = self.clients.pop(client_id, None)
        
        if client:
            logging.info(f"[MediaManager] 客户端 {client_id} 已从字典中移除。")
            if client.pc and client.pc.connectionState != "closed":
                try:
                    await client.pc.close()
                    logging.info(f"[MediaManager] 客户端 {client_id} 的 PeerConnection 已关闭。")
                except Exception as e:
                    logging.warning(f"[MediaManager] 关闭客户端 {client_id} 的 PeerConnection 时出错: {e}")
        else:
            logging.warning(f"[MediaManager] 尝试清理一个不存在的客户端: {client_id}")

        # Remove from track registry
        if self.client_tracks.pop(client_id, None):
            logging.info(f"[MediaManager] 客户端 {client_id} 的 tracks 已从注册表中移除。")

    async def subscribe_existing_tracks(self, new_client_id: str, room_name: str, room_manager):
        """
        新客户端加入房间时，订阅房间内已有客户端的所有tracks
        
        关键流程：
        1. 从track注册表获取已有客户端的tracks
        2. 使用MediaRelay转发这些tracks到新客户端的PeerConnection
        3. 服务器主动发起offer，让新客户端知道有远端流可接收
        4. 新客户端接收offer，创建answer并发回
        
        Args:
            new_client_id: 新加入的客户端ID
            room_name: 房间名称
            room_manager: RoomManager实例，用于获取房间内的其他客户端
        """
        new_client = self.clients.get(new_client_id)
        if not new_client:
            logging.warning(f"subscribe_existing_tracks: 未找到新客户端 {new_client_id}")
            return
        
        # 获取房间内的所有邻居（不包括自己）
        neighbors = room_manager.get_neighbors(new_client_id)
        
        if not neighbors:
            logging.info(f"房间 {room_name} 中没有其他客户端，无需订阅已有流")
            return
        
        logging.info(f"客户端 {new_client_id} 开始订阅房间 {room_name} 中的已有流，邻居数量: {len(neighbors)}")
        
        # 遍历所有邻居，从track注册表获取他们的tracks并转发给新客户端
        tracks_added = 0
        for neighbor_id, neighbor in neighbors.items():
            if neighbor_id == new_client_id:
                continue
            
            # 从track注册表获取邻居的tracks
            neighbor_tracks = self.client_tracks.get(neighbor_id, [])
            logging.info(f"邻居 {neighbor_id} 有 {len(neighbor_tracks)} 个已注册的track")
            
            if not neighbor_tracks:
                continue
            
            # 遍历邻居的所有track并转发
            for track in neighbor_tracks:
                try:
                    # 使用relay订阅track并添加到新客户端的PeerConnection
                    rel_track = self.relay.subscribe(track)
                    
                    # addTrack返回RTCRtpSender，需要通过getTransceivers获取transceiver
                    sender = new_client.pc.addTrack(rel_track)
                    
                    # 获取刚添加的transceiver（最后一个）并设置direction
                    transceivers = new_client.pc.getTransceivers()
                    if transceivers:
                        transceiver = transceivers[-1]
                        transceiver._direction = "sendonly"
                    
                    tracks_added += 1
                    logging.info(f"已将邻居 {neighbor_id} 的 {track.kind} track 转发给新客户端 {new_client_id}")
                except Exception as e:
                    logging.exception(f"转发邻居 {neighbor_id} 的 track 失败: {e}")
        
        # 如果添加了tracks，服务器需要主动发起offer让客户端知道有远端流
        if tracks_added > 0:
            logging.info(f"共为新客户端 {new_client_id} 添加了 {tracks_added} 个track，服务器主动发起offer")
            
            try:
                # 服务器创建offer并发送给新客户端
                offer = await new_client.pc.createOffer()
                await new_client.pc.setLocalDescription(offer)
                
                # 等待ICE gathering完成
                await asyncio.sleep(1.0)
                
                # 发送offer给新客户端
                offer_msg = json.dumps({
                    "type": "offer",
                    "client_id": new_client_id,
                    "sdp": offer.sdp
                })
                await new_client.ws.send(offer_msg)
                logging.info(f"已向新客户端 {new_client_id} 发送offer（包含 {tracks_added} 个远端track）")
            except Exception as e:
                logging.exception(f"为新客户端 {new_client_id} 发起offer失败: {e}")
        else:
            logging.info(f"新客户端 {new_client_id} 没有需要订阅的track")