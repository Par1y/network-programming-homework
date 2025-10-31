import asyncio
import json
import logging
from typing import Optional
from aiohttp import web
import os
import websockets
import aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
from aiortc import RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import MediaStreamError
from PIL import Image
import io
import struct
logging.basicConfig(level=logging.INFO)


class MediaEngine:
    """WebRTC核心"""
    def __init__(self, pc: RTCPeerConnection):
        self.pc = pc
        self.player_video: Optional[MediaPlayer] = None
        self.player_audio: Optional[MediaPlayer] = None
        self._video_device: Optional[str] = None
        self._remote_count = 0
        self._background_tasks = []
        self.stream_queues: dict[int, asyncio.Queue] = {}
        self.audio_stream_queues: dict[int, asyncio.Queue] = {}
        self.remote_tracks: dict[int, aiortc.MediaStreamTrack] = {}
        self.producer_tasks: dict[int, asyncio.Task] = {}
        self.track_id_to_idx: dict[str, int] = {}
        self._current_quality = "medium"
        self._last_bytes_sent = 0
        self._quality_profiles = {
            "low": {"framerate": "15", "video_size": "320x240", "bitrate": 200_000},
            "medium": {"framerate": "25", "video_size": "640x480", "bitrate": 500_000},
            "high": {"framerate": "30", "video_size": "1280x720", "bitrate": 1_500_000}
        }

        pc.on("track")(self._on_track)
        self._start_network_monitor()

    def _start_task(self, coro):
        """
        后台任务创建
        """
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        return task

    def _on_track(self, track):
        """
        处理远程track
        """
        self._remote_count += 1
        idx = self._remote_count
        logging.info(f"收到远端 track kind={track.kind}, id={track.id}, remote_idx={idx}")
        self.remote_tracks[idx] = track
        self.track_id_to_idx[track.id] = idx

        if track.kind == "video":
            self._start_frame_producer_for_track(track, idx)
        elif track.kind == "audio":
            self._start_audio_producer_for_track(track, idx)

    def _start_frame_producer_for_track(self, track, idx):
        """
        track转帧
        """
        logging.info(f"为视频轨道 remote_idx={idx} 创建帧生产者")
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
        self.stream_queues[idx] = q
        logging.info(f"已注册视频队列 remote_idx={idx}。当前视频流: {list(self.stream_queues.keys())}")
        async def produce_frames():
            try:
                while True:
                    frame = await track.recv()
                    arr = frame.to_ndarray(format="rgb24")
                    img = Image.fromarray(arr)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    jpeg = buf.getvalue()
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(jpeg)
            except MediaStreamError:
                logging.info(f"视频流 #{idx} 正常结束 (MediaStreamError)。")
            except asyncio.CancelledError:
                # Task was cancelled, which is expected.
                raise
            except Exception:
                logging.exception(f"处理远端视频流 #{idx} 时发生未知错误。")

        task = self._start_task(produce_frames())
        self.producer_tasks[idx] = task

    def _start_audio_producer_for_track(self, track, idx):
        """
        track转音频帧
        """
        logging.info(f"为音频轨道 remote_idx={idx} 创建帧生产者")
        q: asyncio.Queue[aiortc.AudioFrame] = asyncio.Queue(maxsize=30)
        self.audio_stream_queues[idx] = q
        logging.info(f"已注册音频队列 remote_idx={idx}。当前音频流: {list(self.audio_stream_queues.keys())}")
        async def produce_frames():
            try:
                while True:
                    frame = await track.recv()
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(frame)
            except MediaStreamError:
                logging.info(f"音频流 #{idx} 正常结束 (MediaStreamError)。")
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception(f"处理远端音频流 #{idx} 时发生未知错误。")

        task = self._start_task(produce_frames())
        self.producer_tasks[idx] = task

    def _start_network_monitor(self):
        """
        网络监控轮询
        """
        async def monitor_network_quality():
            await asyncio.sleep(5.0)
            while True:
                await asyncio.sleep(10.0)
                try:
                    await self._check_network_stats()
                except Exception as e:
                    logging.debug(f"网络监控异常: {e}")
        
        self._start_task(monitor_network_quality())

    async def _check_network_stats(self):
        """
        网络分析
        """
        # rtc自带状态监视
        stats = await self.pc.getStats()
        for report in stats.values():
            if report.type == "outbound-rtp" and report.kind == "video":
                self._log_bitrate(report)
            elif report.type == "remote-inbound-rtp" and report.kind == "video":
                await self._adjust_quality_based_on_stats(report)

    def _log_bitrate(self, report):
        """
        码率分析
        """
        bytes_sent = getattr(report, 'bytesSent', 0)
        if self._last_bytes_sent > 0:
            delta_bytes = bytes_sent - self._last_bytes_sent
            current_bitrate = (delta_bytes * 8) / 10.0  # 10秒平均bitrate
            logging.info(f"当前码率: {current_bitrate/1000:.1f} kbps")
        self._last_bytes_sent = bytes_sent

    async def _adjust_quality_based_on_stats(self, report):
        """
        不优雅地用rtt和pl调整画质
        """
        packet_loss = getattr(report, 'fractionLost', 0)
        rtt = getattr(report, 'roundTripTime', 0)
        logging.info(f"网络质量: 丢包率={packet_loss*100:.1f}%, RTT={rtt*1000:.0f}ms")

        new_quality = self._current_quality
        if packet_loss > 0.1 or rtt > 0.3:
            if self._current_quality != "low":
                new_quality = "low"
                logging.warning("网络拥塞严重，降低到低质量")
        elif packet_loss > 0.05 or rtt > 0.15:
            if self._current_quality == "high":
                new_quality = "medium"
                logging.info("网络轻度拥塞，降低到中等质量")
        elif packet_loss < 0.02 and rtt < 0.1:
            if self._current_quality != "high":
                new_quality = "high" if self._current_quality == "medium" else "medium"
                logging.info(f"网络改善，提升到{new_quality}质量")

        if new_quality != self._current_quality:
            await self._adjust_quality(new_quality)

    def setup_local_media(self, video_device: Optional[str] = None, audio_device: Optional[str] = None):
        """
        设置本地采集的视频音频流
        """
        self._setup_video_track(video_device)
        self._setup_audio_track(audio_device)

    def _setup_video_track(self, video_device: Optional[str]):
        """
        本地视频采集
        """
        try:
            profile = self._quality_profiles[self._current_quality]
            options = {"framerate": profile["framerate"], "video_size": profile["video_size"]}
            device = video_device or "/dev/video0"
            self._video_device = device
            
            logging.info(f"尝试打开视频设备 {device} with options {options}")
            self.player_video = MediaPlayer(device, format="v4l2", options=options)
            
            if self.player_video and self.player_video.video:
                self.pc.addTransceiver(self.player_video.video, direction="sendonly")
                logging.info("已添加本地视频 track (addTransceiver sendonly)")
        except Exception:
            # 没有摄像头或者有锁
            logging.warning("无法打开本地视频设备，跳过视频采集")

    def _setup_audio_track(self, audio_device: Optional[str]):
        """
        本地音频采集
        """
        try:
            # pulse
            device = audio_device or "default"
            logging.info(f"尝试打开音频设备 {device}")
            self.player_audio = MediaPlayer(device, format="pulse")
            if self.player_audio and self.player_audio.audio:
                self.pc.addTransceiver(self.player_audio.audio, direction="sendonly")
                logging.info("已添加本地音频 track (addTransceiver sendonly)")
        except Exception:
            logging.warning("无法打开本地音频设备，跳过音频采集")
    
    async def _adjust_quality(self, new_quality: str):
        """
        调整视频质量
        """
        if new_quality not in self._quality_profiles:
            return

        old_player = self.player_video
        if not old_player or not old_player.video:
            logging.warning("质量调整失败：没有找到 video player 或 video track 实例。")
            return

        sender = next((s for s in self.pc.getSenders() if s.track and s.track.kind == "video"), None)
        if not sender:
            logging.warning("质量调整失败：没有找到 video sender。")
            return

        self._current_quality = new_quality
        profile = self._quality_profiles[new_quality]
        target_bitrate = profile['bitrate']
        
        logging.info(f"开始切换视频质量到 {new_quality}: {profile['video_size']}@{profile['framerate']}fps")

        try:
            old_track = old_player.video
            sender.replaceTrack(None)
            await asyncio.sleep(0.2)
            # 操作系统释放设备
            old_track.stop()
            await asyncio.sleep(0.2)

            options = {"framerate": profile["framerate"], "video_size": profile["video_size"]}
            if not self._video_device:
                logging.error("无法找到视频设备路径，无法重启播放器")
                return
            
            device = self._video_device
            new_player = MediaPlayer(device, format="v4l2", options=options)
            await asyncio.sleep(0.5) # 等待播放器完全启动并捕获到第一个关键帧

            sender.replaceTrack(new_player.video)
            self.player_video = new_player
            logging.info("视频轨道替换成功。")

            # 调整新track的编码码率
            try:
                if hasattr(sender, '_stream') and hasattr(sender._stream, '_encoder'):
                    encoder = sender._stream._encoder
                    if hasattr(encoder, 'target_bitrate'):
                        encoder.target_bitrate = target_bitrate
                        logging.info(f"已设置编码器目标码率: {target_bitrate/1000} kbps")
            except Exception as e:
                logging.debug(f"调整编码器参数失败: {e}")

        except Exception as e:
            logging.exception(f"创建新播放器或替换轨道时失败: {e}")

    def _cleanup_track_resources(self, idx: int):
        """
        集中清理失联track资源
        """
        track_id_to_remove = None
        for tid, i in list(self.track_id_to_idx.items()):
            if i == idx:
                track_id_to_remove = tid
                break
        
        if track_id_to_remove and track_id_to_remove in self.track_id_to_idx:
            del self.track_id_to_idx[track_id_to_remove]

        if idx in self.stream_queues:
            del self.stream_queues[idx]
        
        if idx in self.audio_stream_queues:
            del self.audio_stream_queues[idx]

        if idx in self.remote_tracks:
            del self.remote_tracks[idx]

        if idx in self.producer_tasks:
            del self.producer_tasks[idx]

    def handle_track_ended(self, track_id: str):
        """
        通过信令强制结束一个远端轨道处理任务
        """
        idx = self.track_id_to_idx.get(track_id)
        if idx is None:
            logging.warning(f"尝试结束一个未知的轨道，但 track_id {track_id} 在映射中未找到。")
            return

        task = self.producer_tasks.get(idx)
        if task and not task.done():
            task.cancel()
        
        # 立即执行同步清理，消除竞态条件
        self._cleanup_track_resources(idx)

    async def close(self):
        """
        清理，关闭所有连接
        """
        # 关闭本地
        if self.player_video and self.player_video.video:
            try:
                self.player_video.video.stop()
            except Exception:
                pass
        if self.player_audio and self.player_audio.audio:
            try:
                self.player_audio.audio.stop()
            except Exception:
                pass
        
        # 取消并等待后台任务
        for t in self._background_tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except Exception:
                    pass


class SignalingClient:
    """处理 WebSocket 信令和与 MediaEngine 的协作。"""
    def __init__(self, server_ws: str, room_name: str = "testroom"):
        self.server_ws = server_ws
        self.room_name = room_name
        # 构造 RTCConfiguration
        urls = [
            "stun:stun.voipbuster.com:3478",
            "stun:stun.ekiga.net:3478",
            "stun:stun.qq.com:3478",
            "stun:stun.miwifi.com:3478",
            "stun:stun.sipgate.net:3478",
        ]
        ice_servers = [RTCIceServer(urls=[u]) for u in urls]
        config = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration=config)
        self.media = MediaEngine(self.pc)
        self.client_id = None

    async def _send_ice(self, ws, candidate):
        if candidate is None:
            return
        msg = {
            "type": "ice",
            "client_id": self.client_id,
            "candidate": json.dumps({
                "candidate": candidate.candidate,
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
            })
        }
        await ws.send(json.dumps(msg))

    async def run(self):
        """
        主程序，启动！
        """
        try:
            async with websockets.connect(self.server_ws) as ws:
                # 初始化连接，加入房间
                ack_msg = await self._connect_and_join(ws)
                # 初始offer协商
                await self._negotiate_offer(ws, ack_msg)
                # 进running状态，常时信令处理。后续部分均由信令处理。
                await self._message_loop(ws)
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"信令连接关闭: {e}")
        except Exception as e:
            logging.error(f"其他错误: {e}")
        finally:
            logging.info("信令连接关闭，清理媒体资源")
            await self.media.close()

    async def _connect_and_join(self, ws):
        """
        初始化连接并加入房间，对应WebUI连接步骤
        """
        # connect
        await ws.send(json.dumps({"type": "connect"}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "connect_ack":
            raise RuntimeError("没有收到 connect_ack")
        self.client_id = msg.get("client_id")
        logging.info(f"connected, client_id={self.client_id}, rooms={msg.get('rooms')}")

        # ice协商回调
        self.pc.on("icecandidate")(lambda event: asyncio.create_task(self._send_ice(ws, event)))

        # 初始化本地设备
        self.media.setup_local_media()

        # 房间列表
        rooms = msg.get("rooms", [])
        if self.room_name not in rooms:
            await ws.send(json.dumps({"type": "new_room", "room_name": self.room_name}))
            await ws.recv()  # 等待创建房间ack

        await ws.send(json.dumps({"type": "join", "client_id": self.client_id, "room_name": self.room_name}))
        return json.loads(await ws.recv())

    async def _negotiate_offer(self, ws, join_ack):
        """
        主动发送offer，房间为空时宣告自己存在
        """
        if not join_ack.get("server_will_offer", False):
            logging.info("房间空，客户端主动offer")
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            await asyncio.sleep(3.0)  # 等待ICE三秒
            await ws.send(json.dumps({
                "type": "offer",
                "client_id": self.client_id,
                "sdp": self.pc.localDescription.sdp
            }))
            logging.info("客户端已发送offer")
        else:
            logging.info("房间有其他用户，等待服务器发送offer")

    async def _message_loop(self, ws):
        """
        核心信令接收轮询
        """
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await self._dispatch_message(ws, msg)
            except json.JSONDecodeError:
                logging.warning("收到无法解析的消息")

    async def _dispatch_message(self, ws, msg):
        """
        解析信令，处理
        """
        msg_type = msg.get("type")
        if msg_type == "answer":
            await self._handle_answer(msg)
        elif msg_type == "offer":
            await self._handle_offer(ws, msg)
        elif msg_type == "ice":
            await self._handle_ice(msg)
        elif msg_type == "track_ended":
            track_id = msg.get("track_id")
            if track_id:
                logging.info(f"收到服务端 'track_ended' 信号: track_id={track_id}")
                self.media.handle_track_ended(track_id)
        else:
            logging.debug(f"recv msg: {msg}")

    async def _handle_answer(self, msg):
        """
        设置answer
        """
        answer_sdp = msg.get("sdp")
        if answer_sdp:
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
            logging.info("设置远端 answer 完成")

    async def _handle_offer(self, ws, msg):
        """
        处理offer，送回answer
        """
        offer_sdp = msg.get("sdp")
        if offer_sdp:
            try:
                await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
                answer = await self.pc.createAnswer()
                await self.pc.setLocalDescription(answer)
                await asyncio.sleep(3.0) # 等待ICE三秒
                await ws.send(json.dumps({
                    "type": "answer",
                    "client_id": self.client_id,
                    "sdp": self.pc.localDescription.sdp
                }))
                logging.info("已响应服务器offer")
            except Exception as e:
                logging.exception(f"处理服务器offer失败 {e}")

    async def _handle_ice(self, msg):
        """
        处理ICE协商
        """
        candidate_json = msg.get("candidate")
        if candidate_json:
            try:
                cobj = json.loads(candidate_json)
                # 感恩协议
                candidate = RTCIceCandidate(
                    candidate=cobj.get("candidate"),
                    sdpMid=cobj.get("sdpMid"),
                    sdpMLineIndex=cobj.get("sdpMLineIndex"),
                )
                await self.pc.addIceCandidate(candidate)
            except Exception as e:
                logging.exception(f"添加远端 ICE candidate 失败 {e}")



### WebUI

# 全局变量用于持有当前的信令客户端实例和后台任务
current_client: Optional[SignalingClient] = None
client_task: Optional[asyncio.Task] = None

def index(request):
    """
    提供 index.html 页面
    """
    index_path = os.path.join(os.path.dirname(__file__), 'index.html')
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="index.html not found", status=404)

async def api_connect(request):
    """
    连接到信令服务器并加入房间 -> `connect_and_join`
    """
    # 已有
    global current_client, client_task
    if client_task and not client_task.done():
        return web.json_response({"success": False, "message": "已有连接在运行"}, status=400)

    try:
        data = await request.json()
        server_ws = data.get("server_ws")
        room_name = data.get("room_name")
        if not server_ws or not room_name:
            return web.json_response({"success": False, "message": "缺少 server_ws 或 room_name"}, status=400)

        current_client = SignalingClient(server_ws, room_name)
        
        # 在后台任务中运行客户端的主逻辑
        client_task = asyncio.create_task(current_client.run())
        
        # 快速检查任务是否在启动时就失败了
        await asyncio.sleep(0.1)
        if client_task.done() and client_task.exception():
            raise client_task.exception()

        return web.json_response({"success": True, "message": "连接任务已启动"})
    except Exception as e:
        logging.exception("API connect failed")
        return web.json_response({"success": False, "message": str(e)}, status=500)

async def api_disconnect(request):
    """
    断开连接 -> `close`
    """
    global current_client, client_task
    if client_task and not client_task.done():
        client_task.cancel()
        try:
            await client_task
        except asyncio.CancelledError:
            logging.info("客户端任务已取消")
            raise
    
    client_task = None
    current_client = None
    logging.info("API disconnect: 已清理客户端实例。")
    return web.json_response({"success": True, "message": "已断开连接"})

def get_streams(request):
    """
    获取当前可用的媒体流列表
    """
    global current_client
    streams = []
    if current_client and current_client.media:
        for stream_id in current_client.media.stream_queues.keys():
            streams.append({"id": stream_id, "type": "video"})
        for stream_id in current_client.media.audio_stream_queues.keys():
            streams.append({"id": stream_id, "type": "audio"})
    return web.json_response({"success": True, "streams": streams})

async def main():
    app = web.Application()

    # 页面路由
    app.router.add_get('/', index)

    # API 路由
    app.router.add_post('/api/connect', api_connect)
    app.router.add_post('/api/disconnect', api_disconnect)
    app.router.add_get('/api/streams', get_streams)

    # 流路由
    async def mjpeg_handler(request):
        global current_client   # 唉屎山
        stream_id = int(request.match_info["id"])
        
        if not current_client:
            raise web.HTTPNotFound()

        q = current_client.media.stream_queues.get(stream_id)
        if q is None:
            logging.warning(f"MJPEG请求流{stream_id}不存在")
            raise web.HTTPNotFound()

        # 帧
        boundary = "frame"
        resp = web.StreamResponse(status=200, reason='OK', headers={
            'Content-Type': f'multipart/x-mixed-replace; boundary=--{boundary}'
        })
        await resp.prepare(request)

        # 循环写入帧
        try:
            while True:
                jpeg = await q.get()
                await resp.write((f"--{boundary}\r\n").encode())
                await resp.write(b"Content-Type: image/jpeg\r\n\r\n")
                await resp.write(jpeg)
                await resp.write(b"\r\n")
        except asyncio.CancelledError as e:
            logging.info(f"MJPEG流{stream_id}客户端断开 {e}")
            raise
        finally:
            # 确保队列中的数据被消耗，防止队列阻塞
            while not q.empty():
                q.get_nowait()

    app.router.add_get('/stream/{id}', mjpeg_handler)

    async def audio_handler(request):
        """
        音频处理
        纯屎山
        """
        global current_client
        stream_id = int(request.match_info["id"])
        
        if not current_client:
            raise web.HTTPNotFound()

        q = current_client.media.audio_stream_queues.get(stream_id)
        if q is None:
            logging.warning(f"没有这音频啊 {stream_id}")
            raise web.HTTPNotFound()

        resp = web.StreamResponse(status=200, reason='OK', headers={
            'Content-Type': 'audio/wav'
        })
        await resp.prepare(request)

        try:
            # 元数据
            first_frame = await q.get()
            sample_rate = first_frame.sample_rate
            channels = len(first_frame.layout.channels)
            bits_per_sample = first_frame.format.bytes * 8
            
            # 头
            header = struct.pack(
                '<4sI4s4sIHHIIHH4sI',
                b'RIFF', 0, b'WAVE', b'fmt ', 16,
                1, channels, sample_rate,
                sample_rate * channels * (bits_per_sample // 8),
                channels * (bits_per_sample // 8),
                bits_per_sample,
                b'data', 0
            )
            await resp.write(header)
            
            # 最前数据
            await resp.write(first_frame.planes[0].to_bytes())

            while True:
                frame = await q.get()
                await resp.write(frame.planes[0].to_bytes())
        except asyncio.CancelledError:
            logging.info(f"音频连接丢失 {stream_id} ")
        except Exception as e:
            logging.error(f"音频处理出错 {stream_id}: {e}")
        finally:
            # 和视频一样优先处理新的
            while not q.empty():
                q.get_nowait()
            logging.info(f"音频buffer刷新 {stream_id}.")

    app.router.add_get('/audio/{id}', audio_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logging.info("Web server started at http://localhost:8080")
    
    # 保持服务器运行
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
