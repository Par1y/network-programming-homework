import socket
import time
import json
import hashlib
import threading
import os
import logging

# 日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class Send(threading.Thread):
    def __init__(self, name, counter, dir, file, socket, block_size):
        super().__init__()
        self.name = name
        self.counter = counter
        self.dir = dir
        self.file = file
        self.socket = socket
        self.block_size = block_size
    
    def run(self):
        # 发送数据
        checksum = self.checksum()
        self.socket.send(checksum.encode('utf-8'))
        try:
            with open(os.path.join(self.dir, self.file), 'rb') as target_file:
                while(True):
                    block = target_file.read(self.block_size)
                    self.socket.sendall(block)
                    if not block:
                        break
            # 告诉接收端结束了
            self.socket.shutdown(socket.SHUT_WR)
            logging.info(f"传输完成： {self.file}")
        except Exception as e:
            logging.error(f"传输失败： {self.file}, {str(e)}")
    
    def checksum(self) -> str:
        sha1 = hashlib.sha1()
        try:
            with open(os.path.join(self.dir, self.file), 'rb') as target_file:
                while(True):
                    block = target_file.read(self.block_size)
                    if not block:
                        break
                    sha1.update(block)
                return sha1.hexdigest()
        except Exception as e:
            logging.error(f"校验和出错 {e}")

# 监听停止信号
def user_input_thread():
    global go
    while True:
        cmd = input("输入q停止：")
        if cmd.lower() == 'q':
            go = False
            break

def main():
    # 启动一个网络流式套接字
    global go
    try:
        s_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # host = socket.gethostname()
        # 直接监听0.0.0.0了，host不好
        save_dir = str(input("请输入文件目录： "))
        port = int(input("请输入端口： "))
        link_limit = int(input("请输入最大连接数： "))
        password = str(input("口令（留空不设置）： "))
        s_socket.bind(('0.0.0.0', port))
        s_socket.listen(link_limit)
        s_socket.setblocking(False)
    except Exception as e:
        logging.error(f"建立套接字失败 {e}")
    logging.info(f"套接字已建立，正在监听： 0.0.0.0:{port}")

    # 监听结束信号
    stop_input = threading.Thread(target=user_input_thread)
    stop_input.start()

    # 每个连接call一个线程发送文件
    thread_pool = []
    go = True
    addr = ''
    while go:
        try:
            # 建立连接
            x_socket,addr = s_socket.accept()
            pw = json.loads(x_socket.recv(1024).decode('utf-8'))
            if password and pw != password:
                raise ConnectionAbortedError
            files = json.dumps(os.listdir(save_dir))
            x_socket.send(files.encode('utf-8'))
            u_data = json.loads(x_socket.recv(1024).decode('utf-8'))
            name = u_data["name"]
            block_size = u_data["block_size"] or 1024*1024
            thread = Send(
                name=name,
                counter=len(thread_pool),
                dir=save_dir,
                file=name,
                socket=x_socket,
                block_size=block_size
                )
            thread_pool.append(thread)
            thread.start()
        except BlockingIOError:
            time.sleep(0.1)
            continue
        except ConnectionResetError as e:
            logging.error(f"与 {addr} 连接中断 {e}")
            release(x_socket)
        except TimeoutError as e:
            logging.error(f"与 {addr} 连接超时 {e}")
            release(x_socket)
        except ConnectionAbortedError as e:
            logging.error(f"与 {addr} 验证失败 {e}")
            release(x_socket)
        except Exception as e:
            logging.error(f"与 {addr} 连接故障 {e}")
            release(x_socket)

    # 清理
    s_socket.close()
    for t in thread_pool:
        t.join()
        t.socket.close()

def release(socket: socket):
    socket.close()

if __name__ == "__main__":
    main()