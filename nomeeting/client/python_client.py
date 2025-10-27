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
        self.recorders = []
        self._remote_count = 0
        self._background_tasks = []
        self.relay = MediaRelay()
        # 每路远端视频对应的 asyncio.Queue 用于 MJPEG 推送
        self.stream_queues: dict[int, asyncio.Queue] = {}
        # 动态码率控制
        self._current_quality = "medium"
        self._last_bytes_sent = 0
        self._quality_profiles = {
            "low": {"framerate": "15", "video_size": "320x240", "bitrate": 200_000},
            "medium": {"framerate": "30", "video_size": "640x480", "bitrate": 500_000},
            "high": {"framerate": "60", "video_size": "1280x720", "bitrate": 1_500_000}
        }

        @pc.on("track")
        def _on_track(track):
            self._remote_count += 1
            idx = self._remote_count
            kind = track.kind
            logging.info(f"收到远端 track kind={kind}, id={idx}")

            # 根据类型选择输出文件名
            if kind == "video":
                filename = f"remote_video_{idx}.mp4"
            elif kind == "audio":
                filename = f"remote_audio_{idx}.wav"
            else:
                filename = f"remote_{kind}_{idx}.bin"

            recorder = MediaRecorder(filename)
            
            # 使用relay复制track，避免多个消费者竞争
            relayed_track = self.relay.subscribe(track)

            async def start_recorder():
                try:
                    await recorder.start()
                    recorder.addTrack(relayed_track)
                    logging.info(f"开始录制远端 {kind} 到 {filename}")
                except Exception:
                    logging.exception("启动 recorder 失败")
            # 启动 recorder 为后台任务，并保存 task 引用以防被回收
            t = asyncio.create_task(start_recorder())
            self._background_tasks.append(t)
            self.recorders.append(recorder)

            # 如果是视频 track，启动帧读取任务，将 JPEG bytes 放入队列用于 MJPEG 服务
            if kind == "video":
                q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
                self.stream_queues[idx] = q

                async def produce_frames():
                    try:
                        mjpeg_track = self.relay.subscribe(track)
                        while True:
                            frame = await mjpeg_track.recv()
                            arr = frame.to_ndarray(format="rgb24")
                            img = Image.fromarray(arr)
                            buf = io.BytesIO()
                            img.save(buf, format="JPEG")
                            jpeg = buf.getvalue()
                            try:
                                q.put_nowait(jpeg)
                            except asyncio.QueueFull:
                                try:
                                    _ = q.get_nowait()
                                except Exception:
                                    pass
                                try:
                                    q.put_nowait(jpeg)
                                except Exception:
                                    pass
                    except Exception:
                        logging.exception("远端视频帧生产出错")

                t = asyncio.create_task(produce_frames())
                self._background_tasks.append(t)
        
        # 启动带宽监控任务（在_on_track外部）
        async def monitor_network_quality():
            """监控网络质量并动态调整码率"""
            await asyncio.sleep(5.0)  # 等待连接建立
            
            while True:
                await asyncio.sleep(10.0)  # 每10秒检查一次
                
                try:
                    stats = await self.pc.getStats()
                    
                    for report in stats.values():
                        # 监控发送端统计
                        if report.type == "outbound-rtp" and report.kind == "video":
                            bytes_sent = getattr(report, 'bytesSent', 0)
                            
                            # 计算当前码率
                            if self._last_bytes_sent > 0:
                                delta_bytes = bytes_sent - self._last_bytes_sent
                                current_bitrate = (delta_bytes * 8) / 3.0  # bps
                                
                                logging.info(f"当前码率: {current_bitrate/1000:.1f} kbps")
                            
                            self._last_bytes_sent = bytes_sent
                        
                        # 监控接收端反馈（网络质量）
                        if report.type == "remote-inbound-rtp" and report.kind == "video":
                            packet_loss = getattr(report, 'fractionLost', 0)
                            rtt = getattr(report, 'roundTripTime', 0)
                            
                            logging.info(f"网络质量: 丢包率={packet_loss*100:.1f}%, RTT={rtt*1000:.0f}ms")
                            
                            # 动态调整策略
                            new_quality = self._current_quality
                            
                            if packet_loss > 0.1 or rtt > 0.3:  # 丢包>10% 或 RTT>300ms
                                if self._current_quality != "low":
                                    new_quality = "low"
                                    logging.warning("网络拥塞严重，降低到低质量")
                            elif packet_loss > 0.05 or rtt > 0.15:  # 丢包>5% 或 RTT>150ms
                                if self._current_quality == "high":
                                    new_quality = "medium"
                                    logging.info("网络轻度拥塞，降低到中等质量")
                            elif packet_loss < 0.02 and rtt < 0.1:  # 丢包<2% 且 RTT<100ms
                                if self._current_quality == "low":
                                    new_quality = "medium"
                                    logging.info("网络恢复，提升到中等质量")
                                elif self._current_quality == "medium":
                                    new_quality = "high"
                                    logging.info("网络良好，提升到高质量")
                            
                            # 如果需要调整质量
                            if new_quality != self._current_quality:
                                await self._adjust_quality(new_quality)
                
                except Exception as e:
                    logging.debug(f"网络监控异常: {e}")
        
        t = asyncio.create_task(monitor_network_quality())
        self._background_tasks.append(t)

    def setup_local_media(self, video_device: Optional[str] = None, audio_device: Optional[str] = None):
        """尝试创建本地采集，并把 track 添加到 pc。
        """
        try:
            if hasattr(aiortc, "MediaStreamTrack") and hasattr(aiortc.MediaStreamTrack, "from_device"):
                try:
                    logging.info("尝试使用 aiortc.MediaStreamTrack.from_device 打开摄像头")
                    vtrack = aiortc.MediaStreamTrack.from_device(kind="video", device=video_device or "/dev/video0")
                    self.pc.addTrack(vtrack)
                    logging.info("已添加本地视频 track (from_device)")
                except Exception:
                    logging.warning("aiortc.MediaStreamTrack.from_device 失败，回退到 MediaPlayer")
                    raise
            else:
                raise AttributeError
        except Exception:
            try:
                if video_device:
                    logging.info(f"尝试打开视频设备 {video_device}")
                    self.player_video = MediaPlayer(video_device, format="v4l2", options={"framerate": "30"})
                else:
                    # 尝试默认摄像头（若 ffmpeg 支持）
                    self.player_video = MediaPlayer("/dev/video0", format="v4l2")

                if self.player_video and self.player_video.video:
                    self.pc.addTrack(self.player_video.video)
                    logging.info("已添加本地视频 track (MediaPlayer)")
            except Exception:
                logging.warning("无法打开本地视频设备，跳过视频采集")

        try:
            # 音频设备，Linux 下可以尝试 format='pulse' 或 'alsa'，这里使用默认 'pulse' 前提是 ffmpeg 支持
            if audio_device:
                self.player_audio = MediaPlayer(audio_device, format="pulse")
            else:
                self.player_audio = MediaPlayer("default", format="pulse")

            if self.player_audio and self.player_audio.audio:
                self.pc.addTrack(self.player_audio.audio)
                logging.info("已添加本地音频 track")
        except Exception:
            logging.warning("无法打开本地音频设备，跳过音频采集")
    
    async def _adjust_quality(self, new_quality: str):
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
                        # aiortc的RTCRtpSender可能支持有限的参数调整
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
        for recorder in self.recorders:
            try:
                await recorder.stop()
            except Exception:
                logging.exception("停止 recorder 失败")
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

    async def start_mjpeg(self, host: str = "localhost", port: int = 8080):
        """启动本地 aiohttp MJPEG 服务，提供 /, /viewer, /stream/{id}。"""
        app = web.Application()
        
        # 添加CORS中间件
        @web.middleware
        async def cors_middleware(request, handler):
            if request.method == "OPTIONS":
                response = web.Response()
            else:
                response = await handler(request)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = '*'
            return response
        
        app.middlewares.append(cors_middleware)

        async def index(request):
            links = "".join([f'<li><a href="/stream/{k}">stream/{k}</a></li>' for k in self.media.stream_queues.keys()])
            body = f"""<html><body>
                <h1>Streams</h1>
                <p><a href="/viewer">打开视频流查看器</a></p>
                <ul>{links}</ul>
            </body></html>"""
            return web.Response(text=body, content_type="text/html")
        
        async def viewer(request):
            """提供viewer.html页面"""
            import os
            viewer_path = os.path.join(os.path.dirname(__file__), 'viewer.html')
            try:
                with open(viewer_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                return web.Response(text=content, content_type="text/html")
            except FileNotFoundError:
                return web.Response(text="<html><body><h1>viewer.html not found</h1></body></html>",
                                  content_type="text/html", status=404)

        async def mjpeg(request):
            stream_id = int(request.match_info["id"])
            q = self.media.stream_queues.get(stream_id)
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
            except Exception:
                logging.exception(f"MJPEG流{stream_id}处理异常")
            finally:
                try:
                    await resp.write_eof()
                except Exception:
                    pass

            return resp

        app.router.add_get('/', index)
        app.router.add_get('/viewer', viewer)
        app.router.add_get('/stream/{id}', mjpeg)

        self._mjpeg_runner = web.AppRunner(app)
        await self._mjpeg_runner.setup()
        self._mjpeg_site = web.TCPSite(self._mjpeg_runner, host, port)
        await self._mjpeg_site.start()
        logging.info(f'MJPEG server started at http://{host}:{port}')

    async def stop_mjpeg(self):
        try:
            if hasattr(self, '_mjpeg_runner') and self._mjpeg_runner:
                await self._mjpeg_runner.cleanup()
                logging.info('MJPEG server stopped')
        except Exception:
            logging.exception('停止 MJPEG 服务出错')

    async def run(self):
        # 启动 MJPEG 服务
        await self.start_mjpeg()
        
        async with websockets.connect(self.server_ws) as ws:
            # connect
            await ws.send(json.dumps({"type": "connect"}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "connect_ack":
                logging.error("没有收到 connect_ack")
                return
            self.client_id = msg.get("client_id")
            logging.info(f"connected, client_id={self.client_id}, rooms={msg.get('rooms')}")

            # 设置本地 ICE 回调发送到信令服务器
            @self.pc.on("icecandidate")
            def on_icecandidate(event):
                t = asyncio.create_task(self._send_ice(ws, event))
                # 保存任务，防止被垃圾回收
                try:
                    self.media._background_tasks.append(t)
                except Exception:
                    pass

            # 先尝试采集本地媒体（设备可以通过参数/环境调整）
            # setup_local_media 为同步方法，直接调用即可
            try:
                self.media.setup_local_media()
            except Exception:
                logging.exception("尝试设置本地媒体时出错")

            # 创建或加入房间
            rooms = msg.get("rooms", [])
            if isinstance(rooms, str):
                # 处理服务器返回房间字符串的情况，尝试按逗号分隔
                rooms = [room.strip() for room in rooms.split(',')] if rooms else []
            
            # 检查房间是否存在，不存在则创建
            if self.room_name not in rooms:
                await ws.send(json.dumps({"type": "new_room", "room_name": self.room_name}))
                ack = json.loads(await ws.recv())
                logging.info(f"new_room ack: {ack}")
            
            # 加入房间
            await ws.send(json.dumps({"type": "join", "client_id": self.client_id, "room_name": self.room_name}))
            ack = json.loads(await ws.recv())
            logging.info(f"join ack: {ack}")

            # 检查服务器是否会发送offer
            server_will_offer = ack.get("server_will_offer", False)
            
            if server_will_offer:
                # 服务器会发送offer（房间内有已存在的流），客户端等待并响应answer
                logging.info("等待服务器发送offer（房间内有已存在的流）")
            else:
                # 服务器不会发送offer（房间为空），客户端主动发送offer
                logging.info("客户端主动创建offer（房间为空）")
                
                offer = await self.pc.createOffer()
                await self.pc.setLocalDescription(offer)
                
                # 等待ICE gathering完成
                await asyncio.sleep(1.0)
                
                await ws.send(json.dumps({
                    "type": "offer",
                    "client_id": self.client_id,
                    "sdp": self.pc.localDescription.sdp
                }))
                logging.info("客户端已发送offer")

            # 消息接收循环：处理 answer/ice/offer等
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    logging.warning("收到无法解析的消息")
                    continue

                typ = msg.get("type")
                if typ == "answer":
                    answer_sdp = msg.get("sdp")
                    if answer_sdp:
                        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
                        logging.info("设置远端 answer 完成")
                elif typ == "offer":
                    # 服务器主动发起的offer（用于添加新track或新客户端加入）
                    offer_sdp = msg.get("sdp")
                    if offer_sdp:
                        try:
                            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
                            answer = await self.pc.createAnswer()
                            await self.pc.setLocalDescription(answer)
                            # 等待ICE gathering完成
                            await asyncio.sleep(1.0)
                            answer_msg = json.dumps({
                                "type": "answer",
                                "client_id": self.client_id,
                                "sdp": self.pc.localDescription.sdp
                            })
                            await ws.send(answer_msg)
                            logging.info("已响应服务器offer")
                        except Exception:
                            logging.exception("处理服务器offer失败")
                elif typ == "ice":
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
                else:
                    logging.debug(f"recv msg: {msg}")

            # websocket 关闭后清理
            await self.media.close()
            # 停止 MJPEG 服务
            await self.stop_mjpeg()


async def main():
    server = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:3001"
    room = sys.argv[2] if len(sys.argv) > 2 else "testroom"
    client = SignalingClient(server, room)
    await client.run()


if __name__ == "__main__":
    asyncio.run(main())
