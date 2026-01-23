#!/usr/bin/env python3
"""
CFR Decompiler MCP Server (支持 stdio 和 SSE 双模式同时运行)
CFR Decompiler MCP Server (Supports both stdio and SSE modes simultaneously)

Usage:
    stdio mode only (default): python3 cfr_mcp_server.py
    SSE mode only:             python3 cfr_mcp_server.py --mode sse
    Both modes simultaneously: python3 cfr_mcp_server.py --mode both
"""
import re
import sys
import argparse
import asyncio
import logging
import subprocess
import threading
import zipfile
import tempfile
import shutil
from pathlib import Path
from functools import partial

from mcp.server import Server
import mcp.types as types

# --- Configuration (配置) ---
CFR_JAR_PATH = "cfr.jar"
CMD_TIMEOUT_SECONDS = 60
DEFAULT_SSE_HOST = "0.0.0.0"
DEFAULT_SSE_PORT = 8000

# --- Global logger ---
logger = None

# --- Logging (日志) ---
def setup_logging(mode: str):
    """根据模式配置日志输出"""
    global logger
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    if mode == "stdio":
        # stdio 模式：日志输出到 stderr，避免干扰通信
        logging.basicConfig(
            level=logging.DEBUG,
            format=log_format,
            stream=sys.stderr
        )
    else:
        # SSE/both 模式：日志输出到 stdout
        logging.basicConfig(
            level=logging.INFO,
            format=log_format
        )
    
    # 抑制过于详细的日志
    logging.getLogger("anyio").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    logger = logging.getLogger("cfr_server")
    return logger

def validate_option_key(key: str) -> bool:
    """仅允许字母数字键名以防止注入"""
    return bool(re.match(r'^[a-zA-Z0-9]+$', key))

def run_cfr_sync(cmd: list) -> str:
    """同步执行 CFR 命令 (将在线程池中运行)"""
    global logger
    try:
        logger.debug(f"Exec: {' '.join(cmd)}")
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=CMD_TIMEOUT_SECONDS
        )
        
        output = process.stdout
        if process.stderr and len(process.stderr.strip()) > 0:
            output += f"\n\n/*\n[CFR STDERR]\n{process.stderr}\n*/"
        
        logger.debug(f"CFR finished, output: {len(output)} bytes")
        return output
    except subprocess.TimeoutExpired:
        logger.error("CFR execution timed out")
        return "/* Error: Decompilation timed out. (反编译超时) */"
    except Exception as e:
        logger.error(f"CFR execution error: {e}")
        return f"/* Error executing CFR: {str(e)} */"

# --- Smart JAR Scanning Logic ---
def find_classes_in_jar(jar_path: Path, method_name: str) -> list[str]:
    """
    Search for class files in a JAR that might contain the method name.
    Heuristic: Search for method_name bytes in the .class file content.
    在 JAR 中搜索可能包含该方法名的类文件。
    启发式：在 .class 文件内容中搜索方法名的字节序列。
    """
    candidates = []
    # Java method names are UTF-8 encoded in the constant pool
    method_bytes = method_name.encode('utf-8')
    
    try:
        with zipfile.ZipFile(jar_path, 'r') as jar:
            for file_info in jar.infolist():
                if file_info.filename.endswith('.class'):
                    # Read the bytes of the class file
                    with jar.open(file_info) as f:
                        content = f.read()
                        if method_bytes in content:
                            candidates.append(file_info.filename)
    except Exception as e:
        if logger:
            logger.error(f"Error scanning jar: {e}")
        
    return candidates

def find_class_by_name_in_jar(jar_path: Path, class_name: str) -> list[str]:
    """
    Search for class files in a JAR by class name.
    在 JAR 中按类名搜索 class 文件。
    
    Supports:
    - Simple class name: 'BusTypeServiceImpl' -> matches all **/BusTypeServiceImpl.class
    - Full path: 'nc.impl.fts.bustype.BusTypeServiceImpl' -> matches nc/impl/fts/bustype/BusTypeServiceImpl.class
    
    支持：
    - 简单类名: 'BusTypeServiceImpl' -> 匹配所有 **/BusTypeServiceImpl.class
    - 完整路径: 'nc.impl.fts.bustype.BusTypeServiceImpl' -> 精确匹配
    """
    candidates = []
    
    # Determine if it's a full path or simple name
    if '.' in class_name and not class_name.endswith('.class'):
        # Full path: nc.impl.fts.bustype.BusTypeServiceImpl -> nc/impl/fts/bustype/BusTypeServiceImpl.class
        target_path = class_name.replace('.', '/') + '.class'
        exact_match = True
    else:
        # Simple class name
        target_path = class_name.replace('.class', '') + '.class'
        exact_match = False
    
    try:
        with zipfile.ZipFile(jar_path, 'r') as jar:
            for file_info in jar.infolist():
                if file_info.filename.endswith('.class'):
                    if exact_match:
                        # Exact path match
                        if file_info.filename == target_path:
                            candidates.append(file_info.filename)
                    else:
                        # Simple name match: ends with /ClassName.class or equals ClassName.class
                        if file_info.filename.endswith('/' + target_path) or file_info.filename == target_path:
                            candidates.append(file_info.filename)
    except Exception as e:
        if logger:
            logger.error(f"Error scanning jar for class: {e}")
    
    return candidates

def decompile_jar_class_sync(jar_path: Path, class_name: str, base_cmd_args: list) -> str:
    """
    Decompile specific class(es) from a JAR by class name.
    按类名从 JAR 中反编译指定的类。
    """
    candidates = find_class_by_name_in_jar(jar_path, class_name)
    
    if not candidates:
        # Provide helpful error message
        return f"/* Class '{class_name}' not found in {jar_path.name}.\n   Tips: \n   - For simple name, use: 'BusTypeServiceImpl'\n   - For full path, use: 'nc.impl.fts.bustype.BusTypeServiceImpl'\n*/"
    
    if logger:
        logger.info(f"Found {len(candidates)} class(es) matching '{class_name}': {candidates}")
    
    results = []
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        with zipfile.ZipFile(jar_path, 'r') as jar:
            for class_file in candidates:
                try:
                    # Extract the class file
                    extracted_path = jar.extract(class_file, temp_path)
                    
                    # Build command - no --methodname for class decompilation
                    cmd = ["java", "-jar", CFR_JAR_PATH, extracted_path] + base_cmd_args
                    
                    # Run CFR
                    output = run_cfr_sync(cmd)
                    
                    if output and len(output.strip()) > 0:
                        class_header = f"// Source: {class_file}\n"
                        results.append(class_header + output)
                except Exception as e:
                    if logger:
                        logger.warning(f"Failed to decompile class {class_file}: {e}")
                    results.append(f"/* Failed to decompile {class_file}: {e} */")
    
    if not results:
        return f"/* Found {len(candidates)} class(es) but CFR produced no output. */"
    
    return "\n\n".join(results)

def decompile_jar_method_sync(jar_path: Path, method_name: str, base_cmd_args: list) -> str:
    """
    Optimized strategy for JAR files:
    1. Scan JAR for classes containing the method name string.
    2. Extract candidate classes to temp dir.
    3. Decompile candidates individually.
    """
    candidates = find_classes_in_jar(jar_path, method_name)
    
    if not candidates:
        return f"/* Method '{method_name}' not found in any class within {jar_path.name} (scanned {jar_path.stat().st_size} bytes) */"

    if logger:
        logger.info(f"Found {len(candidates)} candidate classes for method '{method_name}'")
    
    results = []
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        with zipfile.ZipFile(jar_path, 'r') as jar:
            for class_file in candidates:
                try:
                    # Extract the class file
                    extracted_path = jar.extract(class_file, temp_path)
                    
                    # Build command for this specific class file
                    # Use the extracted .class file path
                    cmd = ["java", "-jar", CFR_JAR_PATH, extracted_path]
                    
                    # Append user args (methodname, options, etc)
                    # Note: base_cmd_args already includes --methodname
                    cmd.extend(base_cmd_args)
                    
                    # Run CFR
                    output = run_cfr_sync(cmd)
                    
                    # Heuristic check: If CFR outputs code containing the method name
                    if method_name in output: 
                        class_header = f"// Source: {class_file}\n"
                        results.append(class_header + output)
                except Exception as e:
                    if logger:
                        logger.warning(f"Failed to decompile candidate {class_file}: {e}")

    if not results:
        return f"/* Method '{method_name}' matched binary search in {len(candidates)} classes but CFR produced no output. */"
        
    return "\n\n".join(results)

def create_server():
    """创建并配置 MCP 服务器"""
    global logger
    srv = Server("cfr-decompiler")
    
    @srv.list_tools()
    async def list_tools() -> list[types.Tool]:
        """列出可用工具"""
        return [
            types.Tool(
                name="decompile",
                description=(
                    "Decompile a Java class/JAR using CFR. Use this to view the source code of compiled Java files. "
                    "使用 CFR 反编译 Java class/JAR 文件。用于查看编译后的 Java 文件的源代码。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Absolute path to the .class or .jar file. (.class 或 .jar 文件的绝对路径)"
                        },
                        "class_name": {
                            "type": "string",
                            "description": "Class name to decompile from JAR. Supports simple name (e.g. 'BusTypeServiceImpl') or full path (e.g. 'nc.impl.fts.bustype.BusTypeServiceImpl'). (从JAR中按类名反编译，支持简单类名或完整路径)"
                        },
                        "method_name": {
                            "type": "string",
                            "description": "Only decompile methods with this name. For JARs, this triggers a smart search. (仅反编译指定名称的方法。对于 JAR，这将触发智能搜索)"
                        },
                        "ignore_exceptions": {
                            "type": "boolean",
                            "description": "Drop try-catch blocks to make logic clearer. Default: false. (移除 try-catch 块)"
                        },
                        "hide_utf": {
                            "type": "boolean",
                            "description": "Hide UTF-8 characters if encoding is messy. Default: false. (隐藏 UTF-8 字符)"
                        },
                        "options": {
                            "type": "object",
                            "description": "Advanced CFR options (e.g. {'sugarboxing': False}). (高级 CFR 选项)"
                        }
                    },
                    "required": ["file_path"]
                }
            )
        ]

    @srv.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        """处理工具执行"""
        global logger
        logger.info(f"call_tool: {name}, args: {arguments}")
        
        if name != "decompile":
            raise ValueError(f"Unknown tool: {name}")

        file_path_str = arguments.get("file_path")
        class_name = arguments.get("class_name")
        method_name = arguments.get("method_name")

        if not file_path_str:
            raise ValueError("file_path is required")
            
        path = Path(file_path_str).resolve()
        if not path.exists():
            return [types.TextContent(type="text", text=f"Error: File not found: {path}")]

        if not Path(CFR_JAR_PATH).exists():
            return [types.TextContent(type="text", text=f"Error: CFR jar not found at: {CFR_JAR_PATH}")]

        base_args = []
        if arguments.get("ignore_exceptions"):
            base_args.extend(["--ignoreexceptions", "true"])
        if arguments.get("hide_utf"):
            base_args.extend(["--hideutf", "true"])
        base_args.extend(["--comments", "false"])
        base_args.extend(["--showversion", "false"])

        extra_options = arguments.get("options", {})
        for key, value in extra_options.items():
            if not validate_option_key(key):
                continue
            val_str = str(value).lower() if isinstance(value, bool) else str(value)
            base_args.extend([f"--{key}", val_str])

        loop = asyncio.get_running_loop()
        
        if path.suffix.lower() == ".jar":
            if class_name:
                logger.info(f"Decompiling class '{class_name}' from jar '{path.name}'")
                result = await loop.run_in_executor(
                    None, 
                    partial(decompile_jar_class_sync, path, class_name, base_args)
                )
            elif method_name:
                logger.info(f"Smart scan for method '{method_name}' in jar '{path.name}'")
                method_args = base_args + ["--methodname", method_name]
                result = await loop.run_in_executor(
                    None, 
                    partial(decompile_jar_method_sync, path, method_name, method_args)
                )
            else:
                cmd = ["java", "-jar", CFR_JAR_PATH, str(path)] + base_args
                result = await loop.run_in_executor(None, partial(run_cfr_sync, cmd))
        else:
            if method_name:
                base_args.extend(["--methodname", method_name])
            cmd = ["java", "-jar", CFR_JAR_PATH, str(path)] + base_args
            result = await loop.run_in_executor(None, partial(run_cfr_sync, cmd))

        logger.info(f"Returning result: {len(result)} bytes")
        return [types.TextContent(type="text", text=result)]
    
    return srv

# ============================================================
# stdio 模式
# ============================================================
async def run_stdio_mode():
    """运行 stdio 模式服务器"""
    global logger
    from mcp.server.stdio import stdio_server
    
    server = create_server()
    logger.info("CFR MCP Server starting (stdio mode)")
    
    try:
        async with stdio_server() as (read_stream, write_stream):
            logger.info("stdio connected")
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    except Exception as e:
        logger.error(f"stdio server error: {e}")
        raise

# ============================================================
# SSE 模式
# ============================================================
def create_sse_app():
    """创建 SSE 模式的 Starlette 应用"""
    global logger
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.responses import Response
    from mcp.server.sse import SseServerTransport
    
    server = create_server()
    sse = SseServerTransport("/messages")
    
    async def handle_sse(request):
        """处理 SSE 连接"""
        client_info = f"{request.client.host}:{request.client.port}" if request.client else "unknown"
        logger.info(f"SSE connection opened from: {client_info}")
        try:
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                logger.info(f"SSE streams established for: {client_info}")
                await server.run(
                    streams[0], 
                    streams[1], 
                    server.create_initialization_options()
                )
        except Exception as e:
            logger.error(f"SSE connection error for {client_info}: {e}")
            raise
        finally:
            logger.info(f"SSE connection closed for: {client_info}")
        return Response()
    
    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages", app=sse.handle_post_message)
    ])
    
    return app

def run_sse_server(host: str, port: int):
    """在独立线程中运行 SSE 服务器"""
    global logger
    import uvicorn
    
    app = create_sse_app()
    
    # 配置日志格式
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    log_config["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    
    logger.info(f"SSE server starting on http://{host}:{port}/sse")
    uvicorn.run(app, host=host, port=port, log_config=log_config, log_level="info")

def run_sse_mode(host: str, port: int):
    """运行 SSE 模式服务器（阻塞）"""
    print(f"=" * 60)
    print(f"CFR MCP Server (SSE mode)")
    print(f"=" * 60)
    print(f"Host: {host}")
    print(f"Port: {port}")
    print(f"SSE Endpoint: http://{host}:{port}/sse")
    print(f"Messages Endpoint: http://{host}:{port}/messages/")
    print(f"=" * 60)
    
    run_sse_server(host, port)

# ============================================================
# Both 模式 (同时运行 stdio 和 SSE)
# ============================================================
async def run_both_mode(host: str, port: int):
    """同时运行 stdio 和 SSE 模式"""
    global logger
    
    print(f"=" * 60)
    print(f"CFR MCP Server (BOTH modes)")
    print(f"=" * 60)
    print(f"stdio: Listening on stdin/stdout")
    print(f"SSE:   http://{host}:{port}/sse")
    print(f"=" * 60)
    
    # 在后台线程启动 SSE 服务器
    sse_thread = threading.Thread(
        target=run_sse_server,
        args=(host, port),
        daemon=True,
        name="sse-server"
    )
    sse_thread.start()
    logger.info(f"SSE server thread started")
    
    # 给 SSE 服务器一点时间启动
    await asyncio.sleep(1)
    
    # 在主线程运行 stdio 模式
    await run_stdio_mode()

# ============================================================
# 主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="CFR Decompiler MCP Server (支持 stdio、SSE 和双模式同时运行)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  stdio mode only (for OpenCode command mode):
    python3 cfr_mcp_server.py
    python3 cfr_mcp_server.py --mode stdio

  SSE mode only (for HTTP clients):
    python3 cfr_mcp_server.py --mode sse
    python3 cfr_mcp_server.py --mode sse --host 127.0.0.1 --port 9000

  Both modes simultaneously:
    python3 cfr_mcp_server.py --mode both
    python3 cfr_mcp_server.py --mode both --port 8000
"""
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["stdio", "sse", "both"],
        default="stdio",
        help="Server mode: stdio (default), sse, or both"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_SSE_HOST,
        help=f"SSE/both mode: Host to bind (default: {DEFAULT_SSE_HOST})"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_SSE_PORT,
        help=f"SSE/both mode: Port to bind (default: {DEFAULT_SSE_PORT})"
    )
    
    args = parser.parse_args()
    
    # 设置日志
    setup_logging(args.mode)
    
    # 根据模式启动服务器
    if args.mode == "stdio":
        asyncio.run(run_stdio_mode())
    elif args.mode == "sse":
        run_sse_mode(args.host, args.port)
    else:  # both
        asyncio.run(run_both_mode(args.host, args.port))

if __name__ == "__main__":
    main()
