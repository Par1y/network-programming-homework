import os
import re
import json
import threading
import asyncio
import getpass
from tqdm import tqdm
from dataclasses import dataclass
import websockets
from urllib.parse import urlparse

@dataclass
class DirResult:
	status: bool  # 是否合法
	is_remote: bool  # 是否为远程地址
	info: str  # 提示信息/合法地址

def is_remote_address(address: str) -> bool:
	"""判断是否为WebSockets远程地址（ws://host:port/path 或 wss://host:port/path）"""
	ws_pattern = r'^ws[s]?://[a-zA-Z0-9.-]+(:\d+)?(/.*)?$'
	return re.fullmatch(ws_pattern, address) is not None

def get_dir(is_source: bool) -> DirResult:
	prompt = "请输入源目录绝对路径： " if is_source else "请输入目标目录（本地路径或WebSockets地址）： "
	address = input(prompt).strip()

	# 空输入检查
	if not address:
		return DirResult(False, False, "输入不能为空")

	# 远程地址
	if is_remote_address(address):
		return DirResult(True, True, address)
	
	# 本地地址校验
	if not os.path.exists(address):
		return DirResult(False, False, "本地目录不存在")
	if not os.path.isdir(address):
		return DirResult(False, False, "输入不是目录")
	# 源目录需读权限，目标目录需写权限
	if is_source and not os.access(address, os.R_OK):
		return DirResult(False, False, "源目录无读取权限")
	if not is_source and not os.access(address, os.W_OK):
		return DirResult(False, False, "目标目录无写入权限")
	
	return DirResult(True, False,address)

class TransferThread(threading.Thread):
	def __init__(self, source_file: str, target_dir: str, is_remote: bool):
		super().__init__()
		self.source_file = source_file  # 源文件路径
		self.target_dir = target_dir    # 目标地址
		self.is_remote = is_remote      # 是否远程传输
		self.filename = os.path.basename(source_file)
		self.file_size = os.path.getsize(source_file)

	def run(self):
		if self.is_remote:
			asyncio.run(self.remote_transfer())
		else:
			self.local_transfer()

	def local_transfer(self):
		"""本地文件传输"""
		try:
			target_path = os.path.join(self.target_dir, self.filename)
			with tqdm(total=self.file_size, unit="B", unit_scale=True ,desc=self.filename) as pbar:
				block_size = 1024 * 1024  # 1MB块
				with open(self.source_file, "rb") as src, open(target_path, "wb") as dst:
					while chunk := src.read(block_size):
						dst.write(chunk)
						pbar.update(len(chunk))
			print(f"传输完成：{self.filename}")
		except Exception as e :
			print(f"传输失败：{self.filename}，错误：{str(e)}")

	async def remote_transfer(self):
		"""远程传输"""
		try:
			# 解析WebSockets地址
			parsed_url = urlparse(self.target_dir)
			ws_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

			# 建立WebSockets连接
			async with websockets.connect(ws_url) as conn:
				await conn.send(json.dumps({
						"type": "path",
						"path": parsed_url.path
						}))
				# 元数据
				metadata = {
					"type": "metadata",
					"filename": self.filename,
					"size": self.file_size
				}
				await conn.send(json.dumps(metadata))

				resp = await conn.recv()
				resp_data = json.loads(resp)
				if resp_data["status"] != "ok":
					print(f"元数据发送失败：{resp_data['message']}")
					return

				# 发
				with open(self.source_file, "rb") as f, tqdm(total=self.file_size, unit="B", unit_scale=True, desc=self.filename) as pbar:
					block_size = 1024 * 1024
					while chunk := f.read(block_size):
						await conn.send(chunk)
						pbar.update(len(chunk))

				# 结束
				await conn.close()

		except Exception as e:
			print(f"传输失败：{self.filename}，{str(e)}")


def start_transfer(source_dir : str, target_dir : str, is_remote : bool):
	# 获取源目录下所有文件
	files = [os.path.join(source_dir, f) for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
	
	if not files:
		print("源目录无文件")
		return

	# 启动传输线程
	threads = []
	for file in files:
		thread = TransferThread(file, target_dir, is_remote)
		threads.append(thread)
		thread.start()

	# 等待所有线程完成
	for thread in threads:
		thread.join()

def main():
	while True:
		src_result = get_dir(is_source=True)
		if src_result.status:
			source_dir = src_result.info
			break
		print(f"{src_result.info}")

	while True:
		dst_result = get_dir(is_source=False)
		if dst_result.status:
			target_dir = dst_result.info
			is_remote = dst_result.is_remote
			break
		print(f"{dst_result.info}")

	print(f"\n源目录 {source_dir}，目标目录 {target_dir}")
	start_transfer(source_dir, target_dir, is_remote)
	print("传输任务完成")

if __name__ == "__main__":
	main()