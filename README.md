# Xianyu Remote Token Server

从中国大陆网络出口调用闲鱼 IM Token API，解决海外机房风控问题的中转服务。

## 📋 功能

✅ HTTP POST API 接口（FastAPI）
✅ X-API-Key 请求头验证
✅ Cookie 解析 + 用户 ID 提取
✅ 设备 ID 生成 + MD5 签名
✅ 闲鱼 IM Token API 调用
✅ 统一响应格式
✅ 完整日志记录（脱敏处理）
✅ Docker 部署支持
✅ 云函数部署支持
✅ 健康检查 + 连通性测试

## 🚀 快速开始

### Docker 部署

```bash
# 构建镜像
docker build -t xianyu-remote-token-server .

# 运行容器（设置 API_KEY）
docker run -d \
  -e API_KEY=your-32-char-secret-key \
  -e LOG_LEVEL=INFO \
  -e REQUEST_TIMEOUT=30 \
  -p 8000:8000 \
  --name xianyu-token-server \
  xianyu-remote-token-server

# 查看日志
docker logs -f xianyu-token-server

# 测试连通性
curl -X POST http://localhost:8000/test \
  -H "X-API-Key: your-32-char-secret-key"
```

### 本地开发运行

```bash
# 安装依赖
pip install -r requirements.txt

# 设置环境变量
export API_KEY="your-32-char-secret-key"
export LOG_LEVEL="DEBUG"

# 运行应用
python app.py
```

### Docker Compose 部署

在主项目的 `docker-compose.yml` 中添加：

```yaml
xianyu-remote-token-server:
  build:
    context: ./remote-token-service
  container_name: xianyu-token-server
  environment:
    API_KEY: ${REMOTE_TOKEN_API_KEY}
    LOG_LEVEL: INFO
    REQUEST_TIMEOUT: 30
  ports:
    - "8001:8000"
  networks:
    - xianyu-network
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 10s
```

## 📡 API 接口

### 1. 获取 Token - `/invoke`

**请求**

```bash
POST /invoke
Content-Type: application/json
X-API-Key: your-32-char-secret-key

{
  "type": "xianyu_token",
  "data": {
    "cookies": "完整的闲鱼 Cookie 字符串"
  }
}
```

**成功响应 (200 OK)**

```json
{
  "success": true,
  "message": "取Token成功",
  "data": {
    "token": "50000000.9f0e5...",
    "device_id": "550e8400-e29b-41d4-a716-446655440000-unb123456",
    "api_mode": "web"
  }
}
```

**失败响应 (200 OK)**

```json
{
  "success": false,
  "message": "Cookie 中未找到用户标识",
  "data": null
}
```

**错误码**

- `403 Forbidden`：API Key 无效
- `500 Internal Server Error`：服务未配置或内部错误

### 2. 测试连通性 - `/test`

验证 API Key 和基本连接，不需要 Cookie。

**请求**

```bash
POST /test
X-API-Key: your-32-char-secret-key
```

**响应**

```json
{
  "success": true,
  "message": "连通性测试成功，服务正常",
  "data": null
}
```

### 3. 健康检查 - `/health`

负载均衡器和容器编排系统的健康检查端点。

**请求**

```bash
GET /health
```

**响应**

```json
{
  "status": "ok",
  "timestamp": 1724069400.123
}
```

## 🔐 安全配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_KEY` | `""` | **必填**：32 位随机字符串，用于请求验证 |
| `LOG_LEVEL` | `INFO` | 日志级别：DEBUG/INFO/WARNING/ERROR |
| `REQUEST_TIMEOUT` | `30` | 闲鱼 API 请求超时（秒） |
| `LISTEN_HOST` | `0.0.0.0` | 监听地址 |
| `LISTEN_PORT` | `8000` | 监听端口 |

### 日志脱敏

服务日志中**已自动脱敏**以下信息：

- ❌ **不记录** Cookie 字符串
- ❌ **不记录** Token 值
- ❌ **不记录** API Key
- ✅ **记录** 请求方 IP、user_id 前缀、device_id 前缀
- ✅ **记录** 错误类型和调试信息

### 密钥生成

生成 32 位随机密钥：

```bash
# Linux/Mac
openssl rand -hex 16

# Python
python -c "import secrets; print(secrets.token_hex(16))"
```

## 🏃 主平台集成

在主项目的系统设置中配置远程 Token 接口：

```python
# 系统设置表中插入：
INSERT INTO system_setting (key, value) VALUES 
  ('token.remote_url', 'http://xianyu-token-server:8000/invoke'),
  ('token.remote_secret_key', 'your-32-char-secret-key');

# 或在管理后台 -> 系统设置 -> 远程 Token 接口
# 远程 URL: http://xianyu-token-server:8000/invoke
# 秘钥: your-32-char-secret-key

# 点击 "测试连接" 按钮验证
```

## ☁️ 云函数部署

见 `CLOUD_DEPLOYMENT.md`

支持：
- ✅ 阿里云函数计算
- ✅ 腾讯云函数
- ✅ 华为云 FunctionGraph

## 🧪 测试

```bash
chmod +x test.sh
./test.sh
```

## 📊 监控和日志

### 日志格式

```
2026-08-19 10:00:00,123 - __main__ - INFO - 启动闲鱼 Remote Token 服务
2026-08-19 10:00:05,456 - __main__ - INFO - 已生成设备 ID: 550e8400-e29b-41d4... - IP: 192.168.1.100
2026-08-19 10:00:10,789 - __main__ - INFO - Token 获取成功 - IP: 192.168.1.100
2026-08-19 10:00:15,012 - __main__ - WARNING - 非法请求 - IP: 192.168.1.101，API Key: 缺失
```

## 🔧 故障排查

### 问题 1: 连接超时

**症状**：请求超时 (>30s)

**解决**：
1. 增加 `REQUEST_TIMEOUT` 环境变量（如 60）
2. 检查网络连接
3. 查看日志中的具体错误信息

### 问题 2: API Key 无效

**症状**：返回 `403 Forbidden`

**解决**：
1. 确认主平台与远程服务的 API Key 一致
2. 测试连通性：`curl ... /test -H "X-API-Key: your-key"`

### 问题 3: Cookie 解析失败

**症状**：返回 `Cookie 解析失败`

**解决**：
1. 检查从主平台传递的 Cookie 是否完整
2. 使用浏览器复制的原始 Cookie 字符串

## 📈 性能指标

- **镜像大小**：~150-200MB
- **平均响应时间**：~500ms（包含闲鱼 API 调用）
- **并发能力**：100-500（取决于闲鱼 API 限制）
- **内存占用**：150-300MB

## 📝 更新日志

### v1.0.0 (2026-08-19)

- ✅ 初始版本
- ✅ 实现 `/invoke` 端点
- ✅ 实现 `/test` 连通性测试
- ✅ 实现 `/health` 健康检查
- ✅ Docker 和云函数支持
- ✅ 日志脱敏和安全配置

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License

---

**相关链接**：
- 主项目：https://github.com/koookkong90-svg/xianyu-auto-reply
- 闲鱼 API：https://www.goofish.com/
- FastAPI：https://fastapi.tiangolo.com/
