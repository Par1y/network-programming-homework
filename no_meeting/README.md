# NoMeeting - WebRTC 视频会议系统

基于 Python 和 aiortc 的 WebRTC 视频会议系统，支持多人音视频通话。

## 功能特性

- ✅ 多人音视频通话
- ✅ 基于 WebSocket 的信令服务器
- ✅ STUN 服务器支持 NAT 穿透
- ✅ MediaRelay 实现流转发
- ✅ MJPEG HTTP 流查看
- ✅ 响应式 Web UI

## 系统架构

```
┌─────────────┐         ┌─────────────┐         ┌─────────────┐
│  Client A   │◄───────►│   Server    │◄───────►│  Client B   │
│             │         │             │         │             │
│ WebRTC PC   │         │ Room Mgr    │         │ WebRTC PC   │
│ MJPEG HTTP  │         │ Media Mgr   │         │ MJPEG HTTP  │
└─────────────┘         └─────────────┘         └─────────────┘
```

## 安装依赖

```bash
# 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 venv\Scripts\activate  # Windows

# 安装依赖
pip install aiortc aiohttp websockets pillow
```

## 快速开始

### 1. 启动信令服务器

```bash
cd nomeeting/server
python main.py
```

服务器将在 `ws://localhost:3001` 启动。

### 2. 启动客户端

**客户端 A（带摄像头）：**
```bash
cd nomeeting/client
python python_client.py ws://localhost:3001 testroom
```

**客户端 B（无摄像头）：**
```bash
cd nomeeting/client
python python_client.py ws://localhost:3001 testroom
```

每个客户端会：
- 连接到信令服务器
- 加入指定房间
- 尝试打开本地摄像头和麦克风（如果可用）
- 启动 MJPEG HTTP 服务器（默认端口 8080）

### 3. 查看视频流

打开浏览器访问：
```
http://localhost:8080
```

或使用改进的 Web UI：
```
file:///path/to/nomeeting/client/viewer.html
```

Web UI 功能：
- 🔄 自动刷新流列表（每5秒）
- 👁️ 显示/隐藏单个或全部流
- 🪟 在新窗口打开流
- 📱 响应式设计，支持移动设备

## 配置选项

### 环境变量

- `ICE_SERVERS`: 自定义 STUN 服务器列表（逗号分隔）
  ```bash
  export ICE_SERVERS="stun:stun.l.google.com:19302,stun:stun.qq.com:3478"
  ```

### 命令行参数

```bash
python python_client.py [服务器地址] [房间名]
```

示例：
```bash
python python_client.py ws://192.168.1.100:3001 myroom
```

## 文件结构

```
nomeeting/
├── server/
│   ├── main.py              # 服务器入口
│   ├── signal_server.py     # WebSocket 信令服务器
│   ├── room_manager.py      # 房间管理
│   └── media_manager.py     # WebRTC 媒体管理
├── client/
│   ├── python_client.py     # Python 客户端
│   └── viewer.html          # Web UI 查看器
└── README.md
```

## 工作原理

### 信令流程

1. **连接阶段**
   - 客户端连接 WebSocket 服务器
   - 服务器分配唯一 client_id
   - 客户端创建或加入房间

2. **媒体协商**
   - 客户端创建 offer（包含本地媒体描述）
   - 服务器返回 answer
   - 交换 ICE candidates 进行 NAT 穿透

3. **流转发**
   - 服务器接收客户端的 track
   - 使用 MediaRelay 克隆 track
   - 转发给房间内其他客户端
   - 触发重新协商（renegotiation）

### 关键技术点

- **MediaRelay**: 避免多个 PeerConnection 竞争同一 track
- **Non-Trickle ICE**: aiortc 需要 1 秒延迟等待 ICE gathering 完成
- **MJPEG Streaming**: 将 WebRTC 视频帧转换为 HTTP MJPEG 流

## 故障排除

### 无法打开摄像头

```bash
# 检查摄像头设备
ls /dev/video*

# 测试摄像头
ffplay /dev/video0
```

### ICE 连接失败

- 检查防火墙设置
- 确认 STUN 服务器可访问
- 尝试使用不同的 STUN 服务器

### MJPEG 流 404

- 确认客户端已成功接收远端 track
- 检查 `stream_queues` 是否正确创建
- 查看客户端日志中的 track 接收信息

## 已知限制

- 仅支持 Python 客户端（未实现浏览器客户端）
- 服务器不进行媒体处理（纯转发）
- 未实现录制功能（代码中有 MediaRecorder 但未启用）

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！