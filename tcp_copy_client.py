import socket
import time
import json
import hashlib
import os
import re
import logging
from tqdm import tqdm

def checksum(file, block_size, checksum):
    sha1 = hashlib.sha1()
    try:
        with open(file, 'wb') as target_file:
            while(True):
                block = target_file.read(block_size)
                if not block:
                    break
                sha1.update(block)
        if checksum == sha1.hexdigest():
            logging(f"文件哈希匹配 {file}")
        else:
            raise ArithmeticError(f"文件哈希不匹配，文件可能损坏 {file}")
    except Exception as e:
        logging.error(f"校验和出错 {e}")

def main():
    file_path = input("请输入储存文件路径： ")
    addr = input("请输入目标服务器地址端口（host:port）： ")
    addr_split = re.split(r"[:：]", addr)
    host = addr_split[0]
    port = addr_split[1]
    block_size = 1024*1024

    # 建立连接，获得文件列表
    try:
        socket.connect((host, port))
        socket.recv(1024)
        
        socket.send()
    except Exception as e:
        logging.error(f"连接失败 {e}")


    with open(file_path):
        try:
            socket.send()
        except Exception as e:
            pass

    # 接受数据
    try:
        with open(file_path, 'wb') as target_file:
            while(True):
                block = socket.recv(block_size)
                if not block:
                    break
                target_file.write(block)
            checksum()
            logging.info("传输完成： {self.file}")
    except Exception as e:
        logging.error(f"传输失败： {self.file}, {str(e)}")

if __name__ == "__main__":
    main()