# 前言

这是一个基于 Python 的 MCP (Model Context Protocol) 服务器实现，封装了 `cfr.jar` 反编译功能，并支持 SSE (Server-Sent Events) 传输协议。

# 正文

## 1. 环境准备

确保当前目录下有 `cfr.jar`。

安装 Python 依赖：

```Bash
pip install mcp starlette uvicorn
```

## 2. 服务器代码 (`cfr_mcp_server.py`)

该脚本封装了 `cfr.jar`，提供了一个名为 `decompile` 的工具。

> 需要注意修改 CFR_JAR_PATH （cfr.jar 文件路径）

```Python
import os
import re
import asyncio
import logging
import subprocess
import uvicorn
from pathlib import Path
from functools import partial
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import Response
from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types

# --- Configuration (配置) ---
# Path to the CFR jar file
# CFR jar 文件路径
CFR_JAR_PATH = "/path/to/cfr.jar"

# Server Host and Port
# 服务器主机和端口
HOST = "0.0.0.0"
PORT = 8000

# Command execution timeout in seconds
# 命令执行超时时间（秒）
CMD_TIMEOUT_SECONDS = 30 

# --- Logging (日志) ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cfr_server")

# --- Server Instance (服务器实例) ---
server = Server("cfr-decompiler")

def validate_option_key(key: str) -> bool:
    """
    Allow only alphanumeric keys to prevent injection.
    仅允许字母数字键名以防止注入。
    """
    return bool(re.match(r'^[a-zA-Z0-9]+$', key))

def run_cfr_sync(cmd: list) -> str:
    """
    Sync execution wrapper for thread pool.
    用于线程池的同步执行包装器。
    """
    try:
        logger.info(f"Exec: {' '.join(cmd)}")
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=CMD_TIMEOUT_SECONDS
        )
        
        output = process.stdout
        if process.stderr and len(process.stderr.strip()) > 0:
            # Append stderr output as comments
            # 将错误输出作为注释附加
            output += f"\n\n/*\n[CFR STDERR]\n{process.stderr}\n*/"
            
        return output
    except subprocess.TimeoutExpired:
        return "/* Error: Decompilation timed out. File might be too complex or large. (反编译超时，文件可能过大或过于复杂) */"
    except Exception as e:
        return f"/* Error executing CFR (执行 CFR 出错): {str(e)} */"

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    List available tools.
    列出可用工具。
    """
    return [
        types.Tool(
            name="decompile",
            description=(
                "Decompile a Java class/JAR using CFR. "
                "Use this to view the source code of compiled Java files. "
                "使用 CFR 反编译 Java class/JAR 文件。用于查看编译后的 Java 文件的源代码。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the .class or .jar file. (.class 或 .jar 文件的绝对路径)"
                    },
                    "method_name": {
                        "type": "string",
                        "description": "Only decompile methods with this name (highly recommended for large classes). (仅反编译指定名称的方法，强烈建议用于大类)"
                    },
                    "ignore_exceptions": {
                        "type": "boolean",
                        "description": "Drop try-catch blocks to make logic clearer (CFR --ignoreexceptions true). Default: false. (移除 try-catch 块以使逻辑更清晰)"
                    },
                    "hide_utf": {
                        "type": "boolean",
                        "description": "Hide UTF-8 characters if encoding is messy (CFR --hideutf true). Default: false. (如果编码混乱，隐藏 UTF-8 字符)"
                    },
                    "options": {
                        "type": "object",
                        "description": "Advanced CFR options (e.g. {'sugarboxing': False}). Key must be alphanumeric. (高级 CFR 选项，键必须是字母数字)"
                    }
                },
                "required": ["file_path"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """
    Handle tool execution.
    处理工具执行。
    """
    if name != "decompile":
        raise ValueError(f"Unknown tool (未知工具): {name}")

    # 1. Validation (验证)
    file_path_str = arguments.get("file_path")
    if not file_path_str:
        raise ValueError("file_path is required (需要 file_path 参数)")
        
    path = Path(file_path_str).resolve()
    if not path.exists():
        return [types.TextContent(type="text", text=f"Error: File not found (文件未找到): {path}")]

    if not Path(CFR_JAR_PATH).exists():
        return [types.TextContent(type="text", text=f"Error: CFR jar not found at (CFR jar 未找到): {CFR_JAR_PATH}")]

    # 2. Build Command (构建命令)
    cmd = ["java", "-jar", CFR_JAR_PATH, str(path)]
    
    if arguments.get("method_name"):
        cmd.extend(["--methodname", arguments.get("method_name")])
        
    if arguments.get("ignore_exceptions"):
        cmd.extend(["--ignoreexceptions", "true"])
        
    if arguments.get("hide_utf"):
        cmd.extend(["--hideutf", "true"])

    # Defaults (默认设置)
    cmd.extend(["--comments", "false"])    # No header comments (无头部注释)
    cmd.extend(["--showversion", "false"]) # No version info (无版本信息)

    # Advanced Options (高级选项)
    extra_options = arguments.get("options", {})
    for key, value in extra_options.items():
        if not validate_option_key(key):
            logger.warning(f"Ignored invalid option key (忽略无效选项键): {key}")
            continue
        val_str = str(value).lower() if isinstance(value, bool) else str(value)
        cmd.extend([f"--{key}", val_str])

    # 3. Async Execution (异步执行)
    # Run synchronous subprocess in a thread pool to avoid blocking the event loop
    # 在线程池中运行同步 subprocess，避免阻塞事件循环
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, partial(run_cfr_sync, cmd))

    return [types.TextContent(type="text", text=result)]

# --- SSE Transport (SSE 传输) ---
sse = SseServerTransport("/messages")

async def handle_sse(request):
    """
    Handle the initial SSE connection (GET).
    处理初始 SSE 连接 (GET)。
    """
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(
            streams[0], 
            streams[1], 
            server.create_initialization_options()
        )
    return Response()

app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages", app=sse.handle_post_message)
])

if __name__ == "__main__":
    # Log Format Configuration (日志格式配置)
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    log_config["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    
    print(f"Starting Optimized CFR MCP Server on {HOST}:{PORT}")
    print(f"SSE Endpoint: http://{HOST}:{PORT}/sse")
    uvicorn.run(app, host=HOST, port=PORT, log_config=log_config)
```

核心逻辑：

- 使用 `mcp` SDK 和 `Starlette` 处理 SSE。
- 使用 `subprocess` 调用 Java 命令：`java -jar cfr.jar <file> [options]`。
- 捕获标准输出返回给 MCP 客户端。
  - 

## 3. 启动服务器

运行以下命令启动服务（默认端口 8000）：

```Bash
python cfr_mcp_server.py
```

输出示例：

```Plain
➜  cfr_mcp_server python3 cfr_mcp_server.py
Starting CFR MCP Server on 0.0.0.0:8000
SSE Endpoint: http://0.0.0.0:8000/sse
2026-01-09 16:09:56 - INFO - Started server process [1537138]
2026-01-09 16:09:56 - INFO - Waiting for application startup.
2026-01-09 16:09:56 - INFO - Application startup complete.
2026-01-09 16:09:56 - INFO - Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
2026-01-09 16:10:12 - INFO - 127.0.0.1:46308 - "POST /sse HTTP/1.1" 405
2026-01-09 16:10:12 - INFO - 127.0.0.1:46308 - "GET /sse HTTP/1.1" 200
2026-01-09 16:10:12 - INFO - 127.0.0.1:46314 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
2026-01-09 16:10:12 - INFO - 127.0.0.1:46314 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
2026-01-09 16:10:12 - INFO - 127.0.0.1:46314 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
2026-01-09 16:10:12 - INFO - 127.0.0.1:46314 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
2026-01-09 16:10:12 - INFO - 127.0.0.1:46314 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
2026-01-09 16:10:12 - INFO - 127.0.0.1:46314 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
INFO:mcp.server.lowlevel.server:Processing request of type ListToolsRequest
2026-01-09 16:10:15 - INFO - 127.0.0.1:46314 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
2026-01-09 16:10:15 - INFO - 127.0.0.1:46314 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
2026-01-09 16:10:15 - INFO - 127.0.0.1:46320 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
INFO:mcp.server.lowlevel.server:Processing request of type ListPromptsRequest
2026-01-09 16:10:15 - INFO - 127.0.0.1:46314 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
INFO:mcp.server.lowlevel.server:Processing request of type ListResourcesRequest
2026-01-09 16:11:44 - INFO - 127.0.0.1:48686 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
2026-01-09 16:11:44 - INFO - 127.0.0.1:48686 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
INFO:mcp.server.lowlevel.server:Processing request of type ListToolsRequest
2026-01-09 16:11:56 - INFO - 127.0.0.1:56536 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
2026-01-09 16:11:56 - INFO - 127.0.0.1:56536 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
INFO:mcp.server.lowlevel.server:Processing request of type CallToolRequest
2026-01-09 16:11:56 - INFO - Executing CFR command: java -jar /path/to/cfr.jar /path/to/htoa/temp_class/com/oa8000/traceserver/TraceCreateServer.class --comments false
2026-01-09 16:11:57 - INFO - 127.0.0.1:56536 - "POST /messages?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 307
2026-01-09 16:11:57 - INFO - 127.0.0.1:56536 - "POST /messages/?session_id=f951c2ad72a346c18f2c30d1e6d63d9c HTTP/1.1" 202
INFO:mcp.server.lowlevel.server:Processing request of type ListToolsRequest
```

## 4. 客户端配置

### 方式 A: HTTP/SSE 连接 (推荐)

如果你的 MCP 客户端支持通过 URL 连接（如 Cursor 或某些 Web 客户端）：

- **Server** **URL**: `http://localhost:8000/sse`

#### opencode

比如在opencode里mcp部分配置

```JSON
"cfr-decompiler": {
      "type": "remote",
      "url": "http://127.0.0.1:8000/sse",
      "enabled": true,
      "timeout": 300,
      "headers": {
        "Authorization": "Bearer MY_API_KEY"
      },
    },
```

在代码分析过程中，LLM自主决定自动调用反编译class

![](https://image.mrxn.net/7c221bdc54724ef9a3296e09675c1ec7.webp)

指定调用

![](https://image.mrxn.net/aa1595886a8a4a57bc2b3da6a589b58a.webp)

![](https://image.mrxn.net/3aa8d69b73154af393d16b7fc2ffd22d.webp)

#### Cherry Studio

在 MCP服务器 部分配置类型为 服务器发送事件（sse）的mcp服务器即可

![](https://image.mrxn.net/99659e3bfb964f10bfafa79b16a3291d.webp)

![](https://image.mrxn.net/dfe2fcce979e4e99b5c6a98756d5792e.webp)

### 方式 B: 本地命令执行 (Claude Desktop)

> 未测试

在配置文件中添加：

```JSON
{
  "mcpServers": {
    "cfr-decompiler": {
      "command": "python",
      "args": ["/path/to/cfr_mcp_server.py"],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

## 5. 工具使用说明

工具名称: `decompile`

**参数说明:**

| 参数名        | 类型   | 必填 | 说明                                       |
| ------------- | ------ | ---- | ------------------------------------------ |
| `file_path`   | string | 是   | 目标 .class 或 .jar 文件的绝对路径         |
| `method_name` | string | 否   | 指定方法名。只反编译该方法，减少输出干扰。 |
| `options`     | object | 否   | CFR 的高级选项字典。                       |

**调用示例:**

1. **基础反编译**:

```json
{
  "file_path": "/path/to/MyClass.class"
}
```

2. **反编译特定方法 (忽略异常块)**:

```json
{
  "file_path": "/path/to/MyClass.class",
  "method_name": "complexCalculation",
  "options": {
    "ignoreexceptions": true
  }
}
```



