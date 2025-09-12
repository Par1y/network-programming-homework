import os
import re
import subprocess
import paramiko
import tqdm
from dataclasses import dataclass

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

# 远程传输
def remote_transfer(source_dir, target_dir):
    pass

def local_transfer(source_dir, target_dir):
    pass

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
    source_dir = tmp_dir.info

    # 再来获取目标地址
    while(not target_dir):
        tmp_dir = get_dir(True)
        if not tmp_dir.status:
            print(tmp_dir.info)
            tmp_dir = ""
    target_dir = tmp_dir.info

    print(source_dir + "\n" + target_dir)

    # 看看你是不是本地地址
    if target_dir.is_remote:
        remote_transfer(source_dir, target_dir)
    else:
        local_transfer(source_dir, target_dir)

    exit()

if __name__ == "__main__":
    main()