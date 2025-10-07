import aiortc

class MediaManager:
    def __init__(self, room):
        self.room = room
        ice_servers = [{"urls": "stun:stun.nextcloud.com:443"}]  # 公共STUN服务器
        self.pc = aiortc.RTCPeerConnection(ice_servers)
        # 动态绑定signal
        self.pc.on("connectionstatechange", self.on_connection_state_change)
        self.pc.on("track", self.on_track)

    # 断线处理
    async def on_connection_state_change(self):
        if self.pc.connectionState == "failed":
            await self.pc.close()

    async def on_track(self, track, receiver):
        pass

    # 处理客户端offer
    async def offer(self, sdp) -> str:
        offer = aiortc.rtcsessiondescription(sdp=sdp, type="offer")
        await self.pc.setRemoteDescription(offer)

        # 生成SDP Answer
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        return answer.sdp

    # 处理客户端ICE候选
    async def ice(self,candidate):
        await self.pc.addIceCandidate(candidate)