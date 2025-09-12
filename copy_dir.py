import getpass
import os
import re
import threading
import shutil
import paramiko
from tqdm import tqdm
from dataclasses import dataclass

# 传输线程
class Transfer (threading.Thread):
    def __init__(self, name, counter, source_file, target_dir, remote):
        threading.Thread.__init__(self)
        self.name = name
        self.counter = counter
        self.source_file = source_file
        self.target_dir = target_dir
        self.remote = remote
    
    def run(self):
        # 看看你是不是本地地址
        if self.remote:
            self.remote_transfer()
        else:
            self.local_transfer()

    # 远程传输
    def remote_transfer(self):
        ssh = establish_ssh_connection(self.target_dir)
        parsed = re.split(r'[@:]',self.target_dir)
        sftp = ssh.open_sftp()
        # 更新远程目标目录
        self.target_dir = parsed[2]
        total_size = os.path.getsize(self.source_file)
        file_name = os.path.basename(self.source_file)
        try:
            with tqdm(total=total_size, unit="B", unit_scale=True, desc=f"{file_name}") as prog:
                # 丑陋但是有用的callback
                def cb(transferred, total):
                    prog.update(transferred - prog.n)
                # 上传
                sftp.put(self.source_file, os.path.join(self.target_dir, file_name), callback=cb)
        except Exception as e:
            print(f"传输失败： {file_name}, {str(e)}")
        finally:
            sftp.close()
            self.ssh.close()

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
            print(f"传输失败： {file_name}, {str(e)}")

def establish_ssh_connection(target_dir: str):
    parsed = re.split(r'[@:]',target_dir)
    username = parsed[0]
    host = parsed[1]
    ssh = paramiko.SSHClient()
    # 默认添加未知主机
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        password = getpass.getpass(f"输入{username}@{host}的密码： ")
        ssh.connect(hostname=host, username=username, password=password)
        return ssh
    except paramiko.AuthenticationException:
        print("密码错误！")
    except Exception as e:
        print(f"连接失败 {str(e)}")
        exit()

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
        target_dir = input("请输入源目录绝对地址： ").strip()
    else:
        target_dir = input("请输入目标目录绝对地址： ").strip()
    
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

        # 源读取，目标写入
        if type:
            if not os.access(target_dir, os.R_OK):
                return dir_result(False, False, "目录没有读取权限！")
        else:
            if not os.access(target_dir, os.W_OK):
                return dir_result(False, False, "目录没有写入权限！")
        ## 返回合法的本地地址
        return dir_result(True, False, target_dir)

# 每个文件生成一个传输线程
def go_transfer(source_dir: str, target_dir: str, remote: bool):
    files = []
    thread_pool = []
    files = os.listdir(source_dir)
    for i in tqdm(range(len(files))):
        f = files[i]
        source_file = os.path.join(source_dir, f)
        if os.path.isdir(source_file):
            print(f"跳过目录 {f}\n")
        else:
            thread = Transfer(f, i, source_file, target_dir, remote)
            thread_pool.append(thread)
            thread.start()
    for thread in thread_pool:
        thread.join()

# 主函数
def main():
    tmp_dir: dir_result
    source_dir = ""
    target_dir = ""
    # 调用一下获取源地址
    while(not source_dir):
        tmp_dir = get_dir(True)
        if not tmp_dir.status:
            print(tmp_dir.info)
            tmp_dir = ""
        else:
            source_dir = tmp_dir.info

    # 再来获取目标地址
    while(not target_dir):
        tmp_dir = get_dir(False)
        if not tmp_dir.status:
            print(tmp_dir.info)
            tmp_dir = ""
        else:
            target_dir = tmp_dir.info

    # 看看是不是本地地址
    if is_remote_address(target_dir):
        go_transfer(source_dir, target_dir, True)
    else:
        go_transfer(source_dir, target_dir, False)

if __name__ == "__main__":
    main()