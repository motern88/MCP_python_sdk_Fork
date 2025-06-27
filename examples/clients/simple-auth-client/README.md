# 简单认证客户端示例

这是一个演示如何使用 MCP Python SDK 通过 OAuth 认证在 streamable HTTP 或 SSE 传输上进行通信的示例。

## 功能特性

- 支持带 PKCE 的 OAuth 2.0 认证
- 同时支持 StreamableHTTP 和 SSE 传输协议
- 交互式命令行界面

## 安装

```bash
cd examples/clients/simple-auth-client
uv sync --reinstall
```

## 使用方法

### 1. 启动支持 OAuth 的 MCP 服务器

```bash
# 示例使用 mcp-simple-auth
cd path/to/mcp-simple-auth
uv run mcp-simple-auth --transport streamable-http --port 3001
```

### 2. 运行客户端

```bash
uv run mcp-simple-auth-client

# 或者自定义服务器 URL
MCP_SERVER_PORT=3001 uv run mcp-simple-auth-client

# 使用 SSE 传输
MCP_TRANSPORT_TYPE=sse uv run mcp-simple-auth-client
```

### 3. 完成 OAuth 流程

客户端将打开浏览器进行身份验证。完成 OAuth 认证后，您可以使用以下命令：

- `list` - 列出可用工具
- `call <tool_name> [args]` - 调用工具，可选择提供 JSON 参数
- `quit` - 退出

## 示例

```
🔐 Simple MCP Auth Client
Connecting to: http://localhost:3001

Please visit the following URL to authorize the application:
http://localhost:3001/authorize?response_type=code&client_id=...

✅ Connected to MCP server at http://localhost:3001

mcp> list
📋 Available tools:
1. echo - Echo back the input text

mcp> call echo {"text": "Hello, world!"}
🔧 Tool 'echo' result:
Hello, world!

mcp> quit
👋 Goodbye!
```

## 配置

- `MCP_SERVER_PORT` - 服务器端口（默认：8000）
- `MCP_TRANSPORT_TYPE` - 传输类型：`streamable_http`（默认）或 `sse`






# Simple Auth Client Example

A demonstration of how to use the MCP Python SDK with OAuth authentication over streamable HTTP or SSE transport.

## Features

- OAuth 2.0 authentication with PKCE
- Support for both StreamableHTTP and SSE transports
- Interactive command-line interface

## Installation

```bash
cd examples/clients/simple-auth-client
uv sync --reinstall 
```

## Usage

### 1. Start an MCP server with OAuth support

```bash
# Example with mcp-simple-auth
cd path/to/mcp-simple-auth
uv run mcp-simple-auth --transport streamable-http --port 3001
```

### 2. Run the client

```bash
uv run mcp-simple-auth-client

# Or with custom server URL
MCP_SERVER_PORT=3001 uv run mcp-simple-auth-client

# Use SSE transport
MCP_TRANSPORT_TYPE=sse uv run mcp-simple-auth-client
```

### 3. Complete OAuth flow

The client will open your browser for authentication. After completing OAuth, you can use commands:

- `list` - List available tools
- `call <tool_name> [args]` - Call a tool with optional JSON arguments  
- `quit` - Exit

## Example

```
🔐 Simple MCP Auth Client
Connecting to: http://localhost:3001

Please visit the following URL to authorize the application:
http://localhost:3001/authorize?response_type=code&client_id=...

✅ Connected to MCP server at http://localhost:3001

mcp> list
📋 Available tools:
1. echo - Echo back the input text

mcp> call echo {"text": "Hello, world!"}
🔧 Tool 'echo' result:
Hello, world!

mcp> quit
👋 Goodbye!
```

## Configuration

- `MCP_SERVER_PORT` - Server URL (default: 8000)
- `MCP_TRANSPORT_TYPE` - Transport type: `streamable_http` (default) or `sse`
