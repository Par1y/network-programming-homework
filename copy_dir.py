import os
import re
import threading
import shutil
import paramiko
from tqdm import tqdm
from dataclasses import dataclass

# 传输线程
class Transfer (threading.Thread):
    def __init__(self, name, counter, source_file, target_dir):
        threading.Thread.__init__(self)
        self.name = name
        self.counter = counter
        self.source_file = source_file
        self.target_dir = target_dir
    
    def run(self):
        # 看看你是不是本地地址
        if is_remote_address(self.target_dir):
            self.remote_transfer()
        else:
            self.local_transfer()

    # 远程传输
    def remote_transfer(self):
        pass

    # 本地传输
    def local_transfer(self):
        total_size = os.path.getsize(self.source_file)
        file_name = os.path.basename(self.source_file)
        try:
            # 进度条
            with tqdm(total=total_size,unit="B",unit_scale=True,desc=f"{file_name}") as prog:
                # 源和目标文件块
                with open(self.source_file, 'rb') as source_file, open(os.path.join(self.target_dir,file_name), 'wb') as target_file:
                    # 读1M的块，然后写
                    block_size = 1024*1024
                    while True:
                        block = source_file.read(block_size)
                        # 写完了
                        if not block:
                            break
                        target_file.write(block)
                        # 更新进度条
                        prog.update(len(block))
        except Exception as e:
            print(f"传输失败： {file_name}, {e}")



# 地址的返回结构体
@dataclass
class dir_result:
    status: bool
    is_remote: bool
    info: str

# 判断远程 or 本地
def is_remote_address(address: str) -> bool:
    # 正则匹配[user@]host:/path
    ssh_pattern = r'^(?P<user>[a-zA-Z0-9_-]+@)?(?P<host>[a-zA-Z0-9.-]+):(?P<path>/.*)$'
    return re.fullmatch(ssh_pattern, address) is not None

# 获取地址
def get_dir(type: bool) -> dir_result:
    # type, True为源，False为目标
    if type:
        target_dir = input("请输入源目录绝对地址： ")
    else:
        target_dir = input("请输入目标目录绝对地址： ")
    
    if not target_dir:
        return dir_result(False, False, "输入目录为空！")
    
    if is_remote_address(target_dir):
        ## 返回合法的远程地址
        return dir_result(True, True, target_dir)
    else:
        if not os.path.exists(target_dir):
            return dir_result(False, False, "输入目录不存在！")

        if not os.path.isdir(target_dir):
            return dir_result(False, False, "并非目录！")

        if type:
            if not os.access(target_dir, os.R_OK):
                return dir_result(False, False, "目录没有读取权限！")
        else:
            if not os.access(target_dir, os.W_OK):
                return dir_result(False, False, "目录没有写入权限！")
    
    ## 返回合法的本地地址
    return dir_result(True, False, target_dir)

# 主函数
def main():
    tmp_dir: dir_result
    source_dir = ""
    target_dir = ""
    files = []
    thread_pool = []
    # 调用一下获取源地址
    while(not source_dir):
        tmp_dir = get_dir(True)
        print(tmp_dir)
        if not tmp_dir.status:
            print(tmp_dir.info)
            tmp_dir = ""
        else:
            source_dir = tmp_dir.info
    files = os.listdir(source_dir)

    # 再来获取目标地址
    while(not target_dir):
        tmp_dir = get_dir(False)
        if not tmp_dir.status:
            print(tmp_dir.info)
            tmp_dir = ""
        else:
            target_dir = tmp_dir.info

    # 每个文件生成一个传输线程
    for i in tqdm(range(len(files))):
        f = files[i]
        source_file = os.path.join(source_dir, f)
        if os.path.isdir(source_file):
            print(f"跳过目录 {f}\n")
        else:
            thread = Transfer(f, i, source_file, target_dir)
            thread_pool.append(thread)
            thread.start()
    
    for thread in thread_pool:
        thread.join()

    exit()

if __name__ == "__main__":
    main()