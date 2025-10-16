import asyncio
import json
import logging
import sys
from typing import Optional

import websockets
import aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
from aiortc.contrib.media import MediaPlayer, MediaRecorder
from aiohttp import web
from PIL import Image
import io
try:
    from gi.repository import Gst
    Gst.init(None)
except Exception:
    Gst = None

logging.basicConfig(level=logging.INFO)


class MediaEngine:
    """负责本地采集与远端 track 处理（open/closed：可以通过继承或注入替换播放/录制行为）。"""
    def __init__(self, pc: RTCPeerConnection):
        self.pc = pc
        self.player_video: Optional[MediaPlayer] = None
        self.player_audio: Optional[MediaPlayer] = None
        self.recorders = []
        self._remote_count = 0
        self._background_tasks = []
        # 每路远端视频对应的 asyncio.Queue 用于 MJPEG 推送
        self.stream_queues: dict[int, asyncio.Queue] = {}

        @pc.on("track")
        def _on_track(track):
            # 为每条远端 track 创建 recorder（默认保存到文件），也可以替换为实时播放
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

            async def start_recorder():
                try:
                    await recorder.start()
                    recorder.addTrack(track)
                    logging.info(f"开始录制远端 {kind} 到 {filename}")
                except Exception:
                    logging.exception("启动 recorder 失败")
            # 启动 recorder 为后台任务，并保存 task 引用以防被回收
            t = asyncio.create_task(start_recorder())
            self._background_tasks.append(t)
            self.recorders.append(recorder)

            # 如果是视频 track，启动帧读取任务，将 JPEG bytes 放入队列用于 MJPEG 服务
            if kind == "video":
                # 如果系统支持 GStreamer，优先使用 GStreamer appsrc 播放（内部转换为 I420）
                if Gst is not None:
                    pipeline_str = (
                        f"appsrc name=src is-live=true block=true format=time caps=video/x-raw,format=RGB,width=%(w)s,height=%(h)s,framerate=%(fps)s/1 "
                        "! videoconvert ! autovideosink sync=false"
                    )

                    async def gst_playback():
                        try:
                            # 接收一帧以获得宽高和帧率
                            first = await track.recv()
                            h = first.height
                            w = first.width
                            fps = 30

                            # pipeline 构造，使用 I420 caps
                            caps = f"video/x-raw,format=I420,width={w},height={h},framerate={fps}/1"
                            pstr = f"appsrc name=src is-live=true block=true format=time caps={caps} ! videoconvert ! autovideosink sync=false"
                            pipeline = Gst.parse_launch(pstr)
                            appsrc = pipeline.get_by_name("src")
                            pipeline.set_state(Gst.State.PLAYING)

                            pts = 0
                            duration = Gst.util_uint64_scale(1, Gst.SECOND, fps)

                            def push_frame_i420(pyframe):
                                nonlocal pts
                                # 获取 I420 (yuv420p) 数据
                                arr = pyframe.to_ndarray(format="yuv420p")
                                data = arr.tobytes()
                                buf = Gst.Buffer.new_allocate(None, len(data), None)
                                buf.fill(0, data)
                                buf.pts = pts
                                buf.duration = duration
                                pts += duration
                                try:
                                    appsrc.emit("push-buffer", buf)
                                except Exception:
                                    logging.exception("push-buffer 失败")

                            # 推第一个帧
                            push_frame_i420(first)

                            while True:
                                frame = await track.recv()
                                push_frame_i420(frame)
                        except Exception:
                            logging.exception("GStreamer 播放发生错误")
                        finally:
                            try:
                                appsrc.emit("end-of-stream")
                            except Exception:
                                pass
                            try:
                                pipeline.set_state(Gst.State.NULL)
                            except Exception:
                                pass

                    t = asyncio.create_task(gst_playback())
                    self._background_tasks.append(t)
                else:
                    # 回退到 MJPEG 队列方式（浏览器展示）
                    q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
                    self.stream_queues[idx] = q

                    async def produce_frames():
                        try:
                            while True:
                                frame = await track.recv()
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

    def setup_local_media(self, video_device: Optional[str] = None, audio_device: Optional[str] = None):
        """尝试创建本地采集（摄像头/麦克风），并把 track 添加到 pc。

        说明：此方法执行同步初始化（MediaPlayer 构造为同步），不再为 async 函数，
        便于在信令流程中直接调用而无需 await。
        """
        # video_device: eg '/dev/video0' on Linux (format='v4l2')
        # video 采集：优先尝试 aiortc 提供的 from_device（如果存在），否则回退到 MediaPlayer
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
        self.pc = RTCPeerConnection()
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
        """启动本地 aiohttp MJPEG 服务，提供 / 和 /stream/{id}。"""
        app = web.Application()

        async def index(request):
            links = "".join([f'<li><a href="/stream/{k}">stream/{k}</a></li>' for k in self.media.stream_queues.keys()])
            body = f"<html><body><h1>Streams</h1><ul>{links}</ul></body></html>"
            return web.Response(text=body, content_type="text/html")

        async def mjpeg(request):
            stream_id = int(request.match_info["id"])
            q = self.media.stream_queues.get(stream_id)
            if q is None:
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
                raise
            except Exception:
                logging.exception("MJPEG stream error")
            finally:
                try:
                    await resp.write_eof()
                except Exception:
                    pass

            return resp

        app.router.add_get('/', index)
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
        # 启动本地 aiohttp MJPEG 服务，监听 8080
        app = web.Application()

        async def index(request):
            return web.Response(text="<html><body><h1>Streams</h1>\n" + "<ul>" + "".join([f'<li><a href="/stream/{k}">stream/{k}</a></li>' for k in self.media.stream_queues.keys()]) + "</ul></body></html>", content_type="text/html")

        async def mjpeg(request):
            stream_id = int(request.match_info["id"])
            q = self.media.stream_queues.get(stream_id)
            if q is None:
                raise web.HTTPNotFound()

            boundary = "frame"
            headers = {
                "Content-Type": f"multipart/x-mixed-replace; boundary=--{boundary}"
            }

            async def stream_resp(resp):
                while True:
                    try:
                        jpeg = await q.get()
                        await resp.write((f"--{boundary}\r\n").encode())
                        await resp.write(b"Content-Type: image/jpeg\r\n\r\n")
                        await resp.write(jpeg)
                        await resp.write(b"\r\n")
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        break

            app.router.add_get('/', index)
            app.router.add_get('/stream/{id}', mjpeg)

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, 'localhost', 8080)
            await site.start()
            logging.info('MJPEG server started at http://localhost:8080')

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
            rooms = msg.get("rooms") or []
            if isinstance(rooms, str):
                # servers may return rooms as string repr, tolerate it
                rooms = []

            if self.room_name not in rooms:
                await ws.send(json.dumps({"type": "new_room", "room_name": self.room_name}))
                ack = json.loads(await ws.recv())
                logging.info(f"new_room ack: {ack}")

            await ws.send(json.dumps({"type": "join", "client_id": self.client_id, "room_name": self.room_name}))
            ack = json.loads(await ws.recv())
            logging.info(f"join ack: {ack}")

            # 创建本地 offer（此时本地 track 已添加）
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            await ws.send(json.dumps({"type": "offer", "client_id": self.client_id, "sdp": self.pc.localDescription.sdp}))

            # 消息接收循环：处理 answer/ice/其他
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    logging.warning("收到无法解析的消息")
                    continue

                typ = msg.get("type")
                if typ == "offer":
                    # 服务器把 answer 放在 "answer" 字段（兼容性保护）
                    answer_sdp = msg.get("answer") or msg.get("sdp")
                    if answer_sdp:
                        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
                        logging.info("设置远端 answer 完成")
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
            # 停止 aiohttp
            try:
                await runner.cleanup()
            except Exception:
                pass


async def main():
    server = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:3001"
    room = sys.argv[2] if len(sys.argv) > 2 else "testroom"
    client = SignalingClient(server, room)
    await client.run()


if __name__ == "__main__":
    asyncio.run(main())
