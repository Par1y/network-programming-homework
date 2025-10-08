from dataclasses import dataclass
from media_manager import Client
import logging

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

@dataclass
class Room:
    """
    房间类

    `name: str` 房间名

    `clients: dict` 房间内用户 `{ client_id: Client }`
    """
    name: str
    clients: dict

class RoomManager:
    def __init__(self):
        self.rooms = []

    def get_rooms(self) -> list:
        """
        取得房间列表，返回`房间名`列表
        """
        result = []
        for r in self.rooms:
            result.append(r.name)
        return result

    def new_room(self, name: str) -> str:
        """
        新建一个房间
        """
        if not name:
            return "房间名不能为空！"
        n_room = Room(name=name,clients={})
        if n_room not in self.rooms:
            logging.info(f"房间已创建： {name}")
            self.rooms.append(n_room)
            return "success"
        else:
            return "房间已存在！"

    def join(self, client_id: str, room_name: str, client: Client) -> str:
        """
        加入房间
        """
        r: Room
        for room in self.rooms:
            if room_name == room.name:
                r = room
        if r is None:
            return "房间不存在！"
        r.clients[client_id] = client
        return "success"

    def left(self, client_id: str, room_name: list[str]=None) -> str:
        """ 
        离开房间

        `MediaManager` 断线自动处理调用未带`room_name`，故默认所有房间
        """
        try:
            if room_name is None:
                room_name = self.get_rooms
            for room in self.rooms:
                for name in room_name:
                    if name == room.name and client_id in room.clients:
                        del room.clients[client_id]
                        logging.info(f"{client_id} 离开房间 {name}。")
            return "success"
        except Exception as e:
            logging.warning(f"{client_id} 退出房间 {name} 出错！ {e}")
            return f"无法退出 {name} "

    def get_neighbors(self, client_id: str) -> dict:
        """
        找到所有邻居
        """
        # 找到所属房间
        r = {}
        for room in self.rooms:
            if client_id in room.clients:
                # 加入所有邻居
                for c_id, c in room.clients.items():
                    # 去重
                    if c_id not in r:
                        r[c_id] = c
        if r:
            return r
        else:
            return {}