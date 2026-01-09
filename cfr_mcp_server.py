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
CFR_JAR_PATH = "cfr.jar"

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
