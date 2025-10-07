from dataclasses import dataclass
import logging

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

@dataclass
class room:
    name: str
    clients: set

class RoomManager:
    def __init__(self):
        self.rooms = []

    def get_rooms(self) -> list:
        return self.rooms
    
    def new_room(self, name) -> str:
        if not name:
            return "房间名不能为空！"
        n_room = room(name=name,clients={})
        if n_room not in self.rooms:
            logging.info(f"房间已创建： {name}")
            self.rooms.append(n_room)
            return "success"
        else:
            return "房间已存在！"