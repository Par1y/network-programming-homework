import asyncio
import json
import logging
import sys
from typing import Optional
from aiohttp import web
import os
import websockets
import aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
from aiortc import RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer, MediaRecorder, MediaRelay
from PIL import Image
import io
logging.basicConfig(level=logging.INFO)


class MediaEngine:
    """WebRTC核心"""
    def __init__(self, pc: RTCPeerConnection):
        self.pc = pc
        self.player_video: Optional[MediaPlayer] = None
        self.player_audio: Optional[MediaPlayer] = None
        self.recorders = [] # This will be removed logically by removing its usage.
        self._remote_count = 0
        self._background_tasks = []
        self.relay = MediaRelay()
        self.stream_queues: dict[int, asyncio.Queue] = {}
        self._current_quality = "medium"
        self._last_bytes_sent = 0
        self._quality_profiles = {
            "low": {"framerate": "15", "video_size": "320x240", "bitrate": 200_000},
            "medium": {"framerate": "30", "video_size": "640x480", "bitrate": 500_000},
            "high": {"framerate": "60", "video_size": "1280x720", "bitrate": 1_500_000}
        }

        pc.on("track")(self._on_track)
        self._start_network_monitor()

    def _start_task(self, coro):
        """Helper to create and store a background task."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        return task

    def _on_track(self, track):
        """Handles incoming remote media tracks."""
        self._remote_count += 1
        idx = self._remote_count
        logging.info(f"收到远端 track kind={track.kind}, id={idx}")

        # The recorder functionality is no longer needed.
        # self._start_recorder_for_track(track, idx)

        if track.kind == "video":
            self._start_frame_producer_for_track(track, idx)

    # The _start_recorder_for_track method is no longer needed.

    def _start_frame_producer_for_track(self, track, idx):
        """Starts a task to produce JPEG frames for MJPEG streaming."""
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
        self.stream_queues[idx] = q
        mjpeg_track = self.relay.subscribe(track)

        async def produce_frames():
            try:
                while True:
                    frame = await mjpeg_track.recv()
                    arr = frame.to_ndarray(format="rgb24")
                    img = Image.fromarray(arr)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    jpeg = buf.getvalue()
                    
                    # Non-blocking put, discard old frame if full
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(jpeg)
            except Exception:
                logging.exception("远端视频帧生产出错")

        self._start_task(produce_frames())

    def _start_network_monitor(self):
        """Starts the background task for monitoring network quality."""
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
        """Fetches and analyzes WebRTC stats."""
        stats = await self.pc.getStats()
        for report in stats.values():
            if report.type == "outbound-rtp" and report.kind == "video":
                self._log_bitrate(report)
            elif report.type == "remote-inbound-rtp" and report.kind == "video":
                self._adjust_quality_based_on_stats(report)

    def _log_bitrate(self, report):
        """Calculates and logs the current sending bitrate."""
        bytes_sent = getattr(report, 'bytesSent', 0)
        if self._last_bytes_sent > 0:
            delta_bytes = bytes_sent - self._last_bytes_sent
            current_bitrate = (delta_bytes * 8) / 10.0  # bps over 10s
            logging.info(f"当前码率: {current_bitrate/1000:.1f} kbps")
        self._last_bytes_sent = bytes_sent

    def _adjust_quality_based_on_stats(self, report):
        """Adjusts video quality based on packet loss and RTT."""
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
            self._adjust_quality(new_quality)

    def setup_local_media(self, video_device: Optional[str] = None, audio_device: Optional[str] = None):
        """Configures and adds local video and audio tracks to the peer connection."""
        self._setup_video_track(video_device)
        self._setup_audio_track(audio_device)

    def _setup_video_track(self, video_device: Optional[str]):
        """Sets up the local video track using the best available method."""
        try:
            # Prefer the modern from_device method
            if hasattr(aiortc, "MediaStreamTrack") and hasattr(aiortc.MediaStreamTrack, "from_device"):
                logging.info("尝试使用 aiortc.MediaStreamTrack.from_device 打开摄像头")
                vtrack = aiortc.MediaStreamTrack.from_device(kind="video", device=video_device or "/dev/video0")
                self.pc.addTrack(vtrack)
                logging.info("已添加本地视频 track (from_device)")
                return
        except Exception:
            logging.warning("aiortc.MediaStreamTrack.from_device 失败，回退到 MediaPlayer")

        # Fallback to MediaPlayer if from_device is unavailable or fails
        try:
            options = {"framerate": "30"}
            device = video_device or "/dev/video0"
            if video_device:
                logging.info(f"尝试打开视频设备 {device}")
                self.player_video = MediaPlayer(device, format="v4l2", options=options)
            else:
                self.player_video = MediaPlayer(device, format="v4l2", options=options)
            
            if self.player_video and self.player_video.video:
                self.pc.addTrack(self.player_video.video)
                logging.info("已添加本地视频 track (MediaPlayer)")
        except Exception:
            logging.warning("无法打开本地视频设备，跳过视频采集")

    def _setup_audio_track(self, audio_device: Optional[str]):
        """Sets up the local audio track."""
        try:
            device = audio_device or "default"
            logging.info(f"尝试打开音频设备 {device}")
            self.player_audio = MediaPlayer(device, format="pulse")
            if self.player_audio and self.player_audio.audio:
                self.pc.addTrack(self.player_audio.audio)
                logging.info("已添加本地音频 track")
        except Exception:
            logging.warning("无法打开本地音频设备，跳过音频采集")
    
    def _adjust_quality(self, new_quality: str):
        """动态调整视频质量"""
        if new_quality not in self._quality_profiles:
            return
        
        self._current_quality = new_quality
        profile = self._quality_profiles[new_quality]
        target_bitrate = profile['bitrate']
        
        logging.info(f"切换视频质量到 {new_quality}: 目标码率={target_bitrate/1000}kbps")
        
        try:
            # 尝试通过RTCRtpSender调整编码参数
            for sender in self.pc.getSenders():
                if sender.track and sender.track.kind == "video":
                    try:
                        # 尝试访问内部编码器（非标准API）
                        if hasattr(sender, '_stream') and hasattr(sender._stream, '_encoder'):
                            encoder = sender._stream._encoder
                            if hasattr(encoder, 'target_bitrate'):
                                encoder.target_bitrate = target_bitrate
                                logging.info(f"已设置编码器目标码率: {target_bitrate/1000} kbps")
                            else:
                                logging.debug("编码器不支持target_bitrate属性")
                        else:
                            logging.debug("无法访问编码器对象")
                    except Exception as e:
                        logging.debug(f"调整编码器参数失败: {e}")
        except Exception as e:
            logging.debug(f"质量调整失败: {e}")

    async def close(self):
        # 停止所有 recorders
        # The recorder functionality is no longer needed.
        # for recorder in self.recorders:
        #     try:
        #         await recorder.stop()
        #     except Exception:
        #         logging.exception("停止 recorder 失败")
        # 关闭玩家
        if self.player_video:
            try:
                await self.player_video.stop()
            except Exception:
                pass
        if self.player_audio:
            try:
                await self.player_audio.stop()
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
        # 构造 RTCConfiguration, 支持通过环境变量 ICE_SERVERS 指定以逗号分隔的 URL 列表
        env_ices = os.getenv("ICE_SERVERS")
        if env_ices:
            urls = [u.strip() for u in env_ices.split(",") if u.strip()]
        else:
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
        try:
            async with websockets.connect(self.server_ws) as ws:
                ack_msg = await self._connect_and_join(ws)
                await self._negotiate_offer(ws, ack_msg)
                await self._message_loop(ws)
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"信令连接关闭: {e}")
        except Exception as e:
            logging.error(f"SignalingClient run error: {e}")
        finally:
            logging.info("信令连接关闭，开始清理媒体资源...")
            await self.media.close()

    async def _connect_and_join(self, ws):
        """Handles WebSocket connection, client registration, and room joining."""
        await ws.send(json.dumps({"type": "connect"}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "connect_ack":
            raise RuntimeError("没有收到 connect_ack")
        
        self.client_id = msg.get("client_id")
        logging.info(f"connected, client_id={self.client_id}, rooms={msg.get('rooms')}")

        self.pc.on("icecandidate")(lambda event: asyncio.create_task(self._send_ice(ws, event)))
        
        self.media.setup_local_media()

        rooms = msg.get("rooms", [])
        if self.room_name not in rooms:
            await ws.send(json.dumps({"type": "new_room", "room_name": self.room_name}))
            await ws.recv()  # Wait for new_room ack

        await ws.send(json.dumps({"type": "join", "client_id": self.client_id, "room_name": self.room_name}))
        return json.loads(await ws.recv())

    async def _negotiate_offer(self, ws, join_ack):
        """Creates and sends an offer if the room is empty."""
        if not join_ack.get("server_will_offer", False):
            logging.info("客户端主动创建offer（房间为空）")
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            await asyncio.sleep(1.0)  # Wait for ICE gathering
            await ws.send(json.dumps({
                "type": "offer",
                "client_id": self.client_id,
                "sdp": self.pc.localDescription.sdp
            }))
            logging.info("客户端已发送offer")
        else:
            logging.info("等待服务器发送offer（房间内有已存在的流）")

    async def _message_loop(self, ws):
        """Main loop for processing incoming signaling messages."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await self._dispatch_message(ws, msg)
            except json.JSONDecodeError:
                logging.warning("收到无法解析的消息")

    async def _dispatch_message(self, ws, msg):
        """Dispatches messages to appropriate handlers."""
        msg_type = msg.get("type")
        if msg_type == "answer":
            await self._handle_answer(msg)
        elif msg_type == "offer":
            await self._handle_offer(ws, msg)
        elif msg_type == "ice":
            await self._handle_ice(msg)
        else:
            logging.debug(f"recv msg: {msg}")

    async def _handle_answer(self, msg):
        """Handles an SDP answer from the server."""
        answer_sdp = msg.get("sdp")
        if answer_sdp:
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
            logging.info("设置远端 answer 完成")

    async def _handle_offer(self, ws, msg):
        """Handles an SDP offer from the server and sends an answer."""
        offer_sdp = msg.get("sdp")
        if offer_sdp:
            try:
                await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
                answer = await self.pc.createAnswer()
                await self.pc.setLocalDescription(answer)
                await asyncio.sleep(1.0)
                await ws.send(json.dumps({
                    "type": "answer",
                    "client_id": self.client_id,
                    "sdp": self.pc.localDescription.sdp
                }))
                logging.info("已响应服务器offer")
            except Exception:
                logging.exception("处理服务器offer失败")

    async def _handle_ice(self, msg):
        """Handles an ICE candidate from the server."""
        candidate_json = msg.get("candidate")
        if candidate_json:
            try:
                cobj = json.loads(candidate_json)
                candidate = RTCIceCandidate(
                    candidate=cobj.get("candidate"),
                    sdpMid=cobj.get("sdpMid"),
                    sdpMLineIndex=cobj.get("sdpMLineIndex"),
                )
                await self.pc.addIceCandidate(candidate)
            except Exception:
                logging.exception("添加远端 ICE candidate 失败")


# 全局变量用于持有当前的信令客户端实例和后台任务
current_client: Optional[SignalingClient] = None
client_task: Optional[asyncio.Task] = None

def index(request):
    """提供 index.html 页面"""
    index_path = os.path.join(os.path.dirname(__file__), 'index.html')
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="index.html not found", status=404)

async def api_connect(request):
    """API: 连接到信令服务器并加入房间"""
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
    """API: 断开连接"""
    global current_client, client_task
    if client_task and not client_task.done():
        client_task.cancel()
        try:
            await client_task
        except asyncio.CancelledError:
            logging.info("客户端任务已取消")
            raise # Re-raise CancelledError
    
    # The `run` method's finally block will call media.close()
    # No need to call it here again.
    
    client_task = None
    current_client = None
    logging.info("API disconnect: 已清理客户端实例。")
    return web.json_response({"success": True, "message": "已断开连接"})

def get_streams(request):
    """API: 获取当前可用的视频流列表"""
    global current_client
    if current_client and current_client.media:
        stream_ids = list(current_client.media.stream_queues.keys())
        return web.json_response({"success": True, "streams": stream_ids})
    return web.json_response({"success": True, "streams": []})

# The viewer function is no longer needed as the old viewer.html has been replaced.

async def main():
    app = web.Application()

    # 页面路由
    app.router.add_get('/', index)
    # The old /viewer route is no longer needed.

    # API 路由
    app.router.add_post('/api/connect', api_connect)
    app.router.add_post('/api/disconnect', api_disconnect)
    app.router.add_get('/api/streams', get_streams)

    # MJPEG 流路由 (需要动态添加或通过一个集中的处理器)
    # 为了简单起见，我们假设 SignalingClient 启动后会把流注册到一个全局的地方
    # 或者，我们可以改造 mjpeg 函数让它能访问 current_client
    async def mjpeg_handler(request):
        global current_client
        stream_id = int(request.match_info["id"])
        
        if not current_client:
            raise web.HTTPNotFound()

        q = current_client.media.stream_queues.get(stream_id)
        if q is None:
            logging.warning(f"MJPEG请求流{stream_id}不存在")
            raise web.HTTPNotFound()

        boundary = "frame"
        resp = web.StreamResponse(status=200, reason='OK', headers={
            'Content-Type': f'multipart/x-mixed-replace; boundary=--{boundary}'
        })
        await resp.prepare(request)

        try:
            while True:
                jpeg = await q.get()
                await resp.write((f"--{boundary}\r\n").encode())
                await resp.write(b"Content-Type: image/jpeg\r\n\r\n")
                await resp.write(jpeg)
                await resp.write(b"\r\n")
        except asyncio.CancelledError:
            logging.info(f"MJPEG流{stream_id}客户端断开")
            raise
        finally:
            # 确保队列中的数据被消耗，防止队列阻塞
            while not q.empty():
                q.get_nowait()

        return resp

    app.router.add_get('/stream/{id}', mjpeg_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
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
