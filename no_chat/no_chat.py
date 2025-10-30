import sys
import websockets
import asyncio
import logging
import mistune
import time
from dominate.tags import *
from dominate import document
from dominate.util import raw

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class Talk:
    """
    ws聊天
    """
    def __init__(self, host="localhost", port=5500, document=document(), output_file="chat.html"):
        self.host = host
        self.port = port
        self.go = True
        self.is_connected = False
        self.d = document
        self.output_file = output_file

    async def handle_peer(self, websocket: websockets):
        """
        ws连接
        """
        try:
            async for message in websocket:
                msg = mistune.html(message)
                # print("\n" + str(websocket.id) + "  " + msg + "\n输入你想说的内容（两个空行结束）: ", end="", flush=True)
                id = websocket.id
                localtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                self.d.body.add(div(raw(f'<a id="time">{localtime}  </a><a id="uid">{id}</a>: {msg}'), id='income_msg'))
                with open(self.output_file, "w", encoding="utf-8") as f:
                    f.write(self.d.render())
        except Exception as e:
            logging.error(f"{e}")
        finally:
            await websocket.close()

    async def listen(self):
        """
        run server
        """
        logging.info(f"接收监听： ws://{self.host}:{self.port}")
        async with websockets.serve(self.handle_peer, self.host, self.port):
            await asyncio.Future()

    async def conn(self):
        """
        run client
        """
        try:
            addr = await asyncio.to_thread(input, "输入对方地址(ws://host:port)： ")

            async with websockets.connect(addr) as websocket:
                self.is_connected = True
                loop = asyncio.get_running_loop()
                lines = []
                cnt: int = 0
                while self.go:
                    if self.is_connected:
                        print("\n输入你想说的内容（两个空行发送）: ", end="", flush=True)
                        input_data = await loop.run_in_executor(None, sys.stdin.readline)
                        if not input_data:
                            self.go = False
                        if input_data == '\n':
                            cnt += 1
                        if cnt == 2:
                            if lines:
                                message = "".join(lines).strip()
                                logging.info(f"已发送 {message}")
                                msg = mistune.html(message)
                                localtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                                self.d.body.add(div(raw(f'<a id="time">{localtime}</a> {msg}'), id='out_msg'))
                                with open(self.output_file, "w", encoding="utf-8") as f:
                                    f.write(self.d.render())
                                await websocket.send(message)
                            cnt = 0
                            lines.clear()
                        lines.append(input_data)
                    
        except Exception as e:
            logging.error(f"{e}")


async def main():
    d = document()
    d.head += link(rel="stylesheet", href="style.css")
    d += h1('NoChat', id='_title')
    talk = Talk("0.0.0.0", port=3000, document=d, output_file="chat.html")
    listen = asyncio.create_task(talk.listen())
    conn = asyncio.create_task(talk.conn())
    await asyncio.gather(listen, conn)

if __name__ == "__main__":
    asyncio.run(main())