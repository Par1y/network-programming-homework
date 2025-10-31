import aiortc
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
    lock: asyncio.Lock
    joined_event: asyncio.Event

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
        # 用于追踪转发的轨道: { source_client_id: { neighbor_id: [sender, ...] } }
        self.forwarded_tracks = {}
        # 用于处理并发协商
        self.pending_renegotiation = {}

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
            logging.exception("创建 RTCPeerConnection 时使用 RTCConfiguration 失败，回退到默认构造")
            pc = aiortc.RTCPeerConnection()
        self.clients[client_id] = Client(websocket, pc, asyncio.Lock(), asyncio.Event())
        
        @pc.on("iceconnectionstatechange")
        def on_ice_connection_change():
            if pc.iceConnectionState == "connected":
                logging.info(f"ICE已连接 (client_id={client_id})")
            elif pc.iceConnectionState == "failed":
                logging.warning(f"ICE连接失败 (client_id={client_id})")

        @pc.on("connectionstatechange")
        async def on_connection_state_change():
            """
            监控连接状态变化，断开直接扬了。
            """
            logging.info(f"客户端 {client_id} 连接状态变为: {pc.connectionState}")
            if pc.connectionState in ("failed", "closed"):
                logging.warning(f"客户端 {client_id} 的 PeerConnection 状态变为 '{pc.connectionState}'，立即触发清理。")
                # 使用 create_task 在后台执行清理，以避免阻塞当前回调
                _task = asyncio.create_task(self.remove_client(client_id))

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
                logging.warning(f"向客户端 {client_id} 发送 ICE candidate 失败，可能已断开连接。")

        @pc.on("track")
        def on_track(track: any):
            """
            核心流处理
            """
            logging.info(f"收到 track (kind={track.kind}, client_id={client_id})")
            
            current_client = self.clients.get(client_id)
            if not current_client:
                logging.warning(f"on_track: 未找到客户端 (client_id={client_id})")
                return
            
            # 将track注册到track注册表
            if client_id not in self.client_tracks:
                self.client_tracks[client_id] = []
            self.client_tracks[client_id].append(track)
            logging.info(f"客户端 {client_id} 现有 {len(self.client_tracks[client_id])} 个track")

            # 每加入一个新的客户还要重新通知一次 信令触发Event信号这里处理
            async def forward_track_after_join():
                logging.info(f"等待客户端 {client_id} 的 joined_event 信号...")
                await current_client.joined_event.wait()
                logging.info(f"客户端 {client_id} joined_event 已触发，开始转发track")

                neighbors = self.room.get_neighbors(client_id)
                logging.info(f"客户端 {client_id} 的邻居数量: {len(neighbors) if neighbors else 0}")
                
                if not neighbors:
                    logging.warning(f"客户端 {client_id} 没有邻居，track已注册但未转发")
                    return
                
                for neighbor_id, neighbor in neighbors.items():
                    if neighbor_id == client_id:
                        continue
                    
                    if neighbor.pc.connectionState in ("closed", "failed"):
                        logging.warning(f"邻居 {neighbor_id} 的连接状态为 {neighbor.pc.connectionState}，跳过轨道转发")
                        continue
                    
                    try:
                        rel_track = self.relay.subscribe(track)
                        sender = neighbor.pc.addTrack(rel_track)
                        
                        if client_id not in self.forwarded_tracks:
                            self.forwarded_tracks[client_id] = {}
                        if neighbor_id not in self.forwarded_tracks[client_id]:
                            self.forwarded_tracks[client_id][neighbor_id] = []
                        self.forwarded_tracks[client_id][neighbor_id].append(sender)

                        transceivers = neighbor.pc.getTransceivers()
                        if transceivers:
                            transceivers[-1]._direction = "sendonly"
                        
                        logging.info(f"已转发 track 从 {client_id} 到邻居 {neighbor_id}")
                        
                        if neighbor_id not in self.pending_renegotiation:
                            async def debounced_renegotiate():
                                await asyncio.sleep(0.2)
                                self.pending_renegotiation.pop(neighbor_id, None)
                                
                                neighbor_client = self.clients.get(neighbor_id)
                                if not neighbor_client or neighbor_client.pc.connectionState in ("closed", "failed"):
                                    logging.warning(f"邻居 {neighbor_id} 已断开，取消协商。")
                                    return

                                logging.info(f"开始为邻居 {neighbor_id} 进行重新协商。")
                                if neighbor_client.pc.signalingState == "stable":
                                    try:
                                        offer = await neighbor_client.pc.createOffer()
                                        await neighbor_client.pc.setLocalDescription(offer)
                                        
                                        offer_msg = json.dumps({
                                            "type": "offer",
                                            "sdp": offer.sdp
                                        })
                                        await neighbor_client.ws.send(offer_msg)
                                        logging.info(f"已向邻居 {neighbor_id} 发送Offer。")
                                    except Exception as e:
                                        logging.exception(f"为邻居 {neighbor_id} 发送Offer失败: {e}")
                                else:
                                    logging.warning(f"邻居 {neighbor_id} 的信令状态为 {neighbor_client.pc.signalingState}，跳过防抖动的重新协商。")

                            task = asyncio.create_task(debounced_renegotiate())
                            self.pending_renegotiation[neighbor_id] = task
                            self._background_tasks.append(task)
                        
                    except Exception:
                        logging.exception(f"转发 track 失败 (neighbor_id={neighbor_id})")

            # 总之异步防止阻塞
            task = asyncio.create_task(forward_track_after_join())
            self._background_tasks.append(task)

    async def offer(self, client_id: str, sdp: any) -> str:
        """
        处理客户端offer
        """
        c = self.clients.get(client_id)
        if c is None:
            logging.warning(f"offer: 未找到 client_id={client_id}")
            raise ValueError("client 未注册")
        
        # 如果信令状态不是 stable，说明服务器正在进行协商
        # 此时应忽略客户端的offer，以避免状态冲突。
        if c.pc.signalingState != "stable":
            logging.warning(f"忽略来自客户端 {client_id} 的offer，因为当前信令状态为 '{c.pc.signalingState}'（非 'stable'）")
            return

        offer = aiortc.RTCSessionDescription(sdp=sdp, type="offer")
        await c.pc.setRemoteDescription(offer)

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
        """
        清理
        """
        client_to_remove = self.clients.get(client_id)
        if not client_to_remove:
            logging.info(f"[MediaManager] 客户端 {client_id} 已被清理，跳过重复操作。")
            return

        async with client_to_remove.lock:
            # 已经被其他协程清理
            if client_id not in self.clients:
                logging.info(f"[MediaManager] 客户端 {client_id} 在获取锁后发现已被清理，跳过。")
                return
            
            logging.info(f"[MediaManager] 开始清理客户端: {client_id}")


            # 所有流爬
            forwarded_from_client = self.forwarded_tracks.pop(client_id, {})
            if forwarded_from_client:
                logging.info(f"开始通知 {len(forwarded_from_client)} 个邻居停止接收来自 {client_id} 的轨道。")
                for neighbor_id, senders_to_remove in forwarded_from_client.items():
                    neighbor_client = self.clients.get(neighbor_id)
                    if not neighbor_client or neighbor_client.pc.connectionState in ("closed", "failed"):
                        logging.warning(f"邻居 {neighbor_id} 连接已关闭或不存在，跳过轨道停止。")
                        continue

                    removed_count = 0
                    for sender in senders_to_remove:
                        if sender and sender.track:
                            # 草，发送者id
                            track_id = sender.track.id
                            
                            # 有人死了！！！
                            try:
                                end_msg = json.dumps({"type": "track_ended", "track_id": track_id})
                                asyncio.create_task(neighbor_client.ws.send(end_msg))
                                logging.info(f"已向邻居 {neighbor_id} 发送 track_ended 信号 (track_id: {track_id})")
                            except Exception as e:
                                logging.warning(f"向邻居 {neighbor_id} 发送 track_ended 信号失败: {e}")

                            # 别发了
                            try:
                                sender.replaceTrack(None)
                                removed_count += 1
                            except Exception as e:
                                logging.warning(f"从邻居 {neighbor_id} 停止轨道时出错: {e}")
                        else:
                            # 异常情况处理
                            try:
                                if sender:
                                    sender.replaceTrack(None)
                                    removed_count += 1
                            except Exception as e:
                                logging.warning(f"从邻居 {neighbor_id} 停止一个未知轨道时出错: {e}")

                    if removed_count > 0:
                        logging.info(f"已停止向邻居 {neighbor_id} 发送 {removed_count} 个轨道。")

            # 该客户为目标的流爬
            for source_id in list(self.forwarded_tracks.keys()):
                if client_id in self.forwarded_tracks.get(source_id, {}):
                    self.forwarded_tracks[source_id].pop(client_id)
                    logging.info(f"已清理从 {source_id} 到 {client_id} 的转发记录。")

            # PC爬
            if client_to_remove.pc.connectionState != "closed":
                try:
                    await client_to_remove.pc.close()
                    logging.info(f"[MediaManager] 客户端 {client_id} 的 PeerConnection 已关闭。")
                except Exception as e:
                    logging.warning(f"[MediaManager] 关闭客户端 {client_id} 的 PeerConnection 时出错: {e}")

            # 滚出去
            if self.clients.pop(client_id, None):
                logging.info(f"[MediaManager] 客户端 {client_id} 已从字典中移除。")
            
            if self.client_tracks.pop(client_id, None):
                logging.info(f"[MediaManager] 客户端 {client_id} 的 tracks 已从注册表中移除。")
            
            # 爬爬爬
            self.room.left(client_id)
            logging.info(f"[MediaManager] 已通知 RoomManager 客户端 {client_id} 离开。")

    async def subscribe_existing_tracks(self, new_client_id: str, room_name: str, room_manager):
        """
        新客户端加入房间时，订阅房间内已有客户端的所有tracks
        `new_client_id`: 新加入的客户端ID
        `room_name`: 房间名称
        `room_manager`: RoomManager实例，用于获取房间内的其他客户端
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

                    # 注册转发轨道，确保清理时能正确通知
                    if neighbor_id not in self.forwarded_tracks:
                        self.forwarded_tracks[neighbor_id] = {}
                    if new_client_id not in self.forwarded_tracks[neighbor_id]:
                        self.forwarded_tracks[neighbor_id][new_client_id] = []
                    self.forwarded_tracks[neighbor_id][new_client_id].append(sender)
                    
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
                logging.warning(f"为新客户端 {new_client_id} 发起offer失败，可能已断开连接: {e}")
        else:
            logging.info(f"新客户端 {new_client_id} 没有需要订阅的track")
