import asyncio
import websockets
import json
import os
from pathlib import Path
from typing import Optional

async def handle_client(websocket: any):
    try:
        path = "/tmp"
        _path = json.loads(await websocket.recv())
        if _path.get("type") == "path":
            path = _path.get("path")

        # 解析目标目录
        target_dir = Path(path).resolve()
        
        if not target_dir.exists():
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                await websocket.send(json.dumps({
                    "status": "error",
                    "message": f"无法创建目录: {str(e)}"
                }))
                return
        
        if not os.access(target_dir, os.W_OK):
            await websocket.send(json.dumps({
                "status": "error",
                "message": "目标目录无写入权限"
            }))
            return

        metadata_msg = await websocket.recv()
        metadata = json.loads(metadata_msg)
        
        # 乱序了
        if metadata.get("type") != "metadata":
            await websocket.send(json.dumps({
                "status": "error",
                "message": "请先发送文件元数据"
            }))
            return

        filename: str = metadata.get("filename")
        file_size: int = metadata.get("size")
        
        if not filename or not file_size:
            await websocket.send(json.dumps({
                "status": "error",
                "message": "元数据丢失"
            }))
            return

        target_file = target_dir / filename
        if target_file.exists():
            await websocket.send(json.dumps({
                "status": "error",
                "message": "文件已存在"
            }))
            return

        await websocket.send(json.dumps({
            "status": "ok",
            "message": "元数据接收成功，等待文件内容"
        }))

        # 接收文件
        received_size = 0
        with open(target_file, "wb") as f:
            while received_size < file_size:
                try:
                    data = await websocket.recv()
                    
                    # 结束
                    if isinstance(data, str):
                        end_signal = json.loads(data)
                        if end_signal.get("type") == "end":
                            break

                    # 写入文件
                    f.write(data)
                    received_size += len(data)

                except websockets.exceptions.ConnectionClosed:
                    print(f"连接中断：{filename} 仅接收 {received_size}/{file_size} 字节")
                    break

    except Exception as e:
        await websocket.send(json.dumps({
            "status": "error",
            "message": f"服务端错误：{str(e)}"
        }))
    finally:
        await websocket.close()

async def run_server():
    """启动WebSockets服务端"""
    async with websockets.serve(handle_client, "0.0.0.0", 8765):
        print("服务端已启动，监听 ws://0.0.0.0:8765")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(run_server())