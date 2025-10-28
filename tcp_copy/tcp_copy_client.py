import socket
import json
import hashlib
import os
import re
import logging
from tqdm import tqdm

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


def checksum(file, block_size, file_checksum):
    sha1 = hashlib.sha1()
    try:
        with open(file, 'rb') as target_file:
            while(True):
                block = target_file.read(block_size)
                if not block:
                    break
                sha1.update(block)
        if file_checksum == sha1.hexdigest():
            logging.info(f"文件哈希匹配 {file}")
        else:
            raise ArithmeticError(f"文件哈希不匹配，文件可能损坏 {file}")
    except Exception as e:
        logging.error(f"校验和出错 {e}")

def main():
    file_path = input("请输入储存文件路径： ")
    addr = input("请输入目标服务器地址端口（host:port）： ")
    addr_split = re.split(r"[:：]", addr)
    host = addr_split[0]
    port = int(addr_split[1])
    block_size = 1024*1024

    # 建立连接，获得文件列表
    try:
        c_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c_socket.connect((host, port))
        files = json.loads(c_socket.recv(1024))
        logging.info(f"文件列表： {files}\n")
        file = str(input("请输入需下载文件名： "))
        msg = {"name":file, "block_size":block_size}
        pre_json = json.dumps(msg)
        c_socket.send(pre_json.encode('utf-8'))
        file_checksum = c_socket.recv(1024).decode('utf-8')
    except Exception as e:
        logging.error(f"连接失败 {e}")
        return

    # 接受数据
    try:
        with open(os.path.join(file_path, file), 'wb') as target_file:
            while(True):
                block = c_socket.recv(block_size)
                if not block:
                    break
                target_file.write(block)
        checksum(os.path.join(file_path, file), block_size, file_checksum)
        logging.info(f"传输完成： {file}")
    except Exception as e:
        logging.error(f"传输失败： {file}, {str(e)}")

if __name__ == "__main__":
    main()