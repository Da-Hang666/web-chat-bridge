"""
web-chat-bridge MCP Server v4.0.0

标准 MCP Server，将 v3 的所有 HTTP endpoint 映射为 MCP Tools。
同时保留 --serve 模式向后兼容 HTTP API。

用法：
  # MCP stdio 模式（供 Cline/CodeWhale/Cursor 等 MCP 客户端调用）
  python server.py

  # HTTP daemon 模式（向后兼容）
  python server.py --serve [--port PORT]

  # 初始化浏览器会话
  python server.py --init [--url URL] [--site SITE]

  # 发送消息 / 评审（通过 daemon 或直接模式）
  python server.py --send "消息"
  python server.py --review "文本"
  python server.py --review-file path/to/file.py
  python server.py --review-image path/to/image.png

  # 缓存管理
  python server.py --cache-info

  # 也可以通过 mcp install 安装到 MCP 客户端
  # pip install mcp && python -m mcp install server.py
"""

import argparse
import atexit
import io
import json
import sys
import time
from pathlib import Path

# Windows 中文环境：强制 stdout/stderr 输出 UTF-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── MCP SDK ──
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

# ── 项目模块 ──
from config import DAEMON_PORT, SESSION_FILE, CACHE_FILE, SCREENSHOT_DIR, SITE_CONFIGS
import cache as cache_module
from daemon import (
    daemon_send_message, daemon_do_review, daemon_review_image,
    daemon_screenshot, daemon_navigate, daemon_click,
    daemon_scroll, daemon_read, daemon_type_text, daemon_execute,
    start_daemon, _call_daemon, _daemon_config, _daemon_session,
)
from browser_controller import init_browser
from review_engine import do_review, single_send, parse_review_json


# ═══════════════════════════════════════════════════════════════
# MCP Server 实例
# ═══════════════════════════════════════════════════════════════

server = Server("web-chat-bridge")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="send_message",
            description="向聊天平台（DeepSeek Chat/豆包等）发送消息并获取 AI 回复。支持人类化逐字打字和长文本粘贴。",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "要发送给 AI 的消息文本"},
                    "site": {"type": "string", "description": "站点标识 (deepseek/doubao等)", "default": "deepseek"},
                },
                "required": ["message"],
            },
        ),
        types.Tool(
            name="review_text",
            description="评审文本/代码质量。通过 Actor-Critic 系统将产出发给 AI 模型进行结构化评审，返回评分、问题和改进建议。自动缓存相同内容的评审结果。",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要评审的文本/代码内容"},
                    "context": {"type": "string", "description": "评审的附加上下文说明", "default": ""},
                    "mode": {"type": "string", "description": "模型模式 (fast/expert/vision)", "default": "expert"},
                    "cache_bypass": {"type": "boolean", "description": "是否跳过缓存强制重新评审", "default": False},
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="review_image",
            description="上传图片到 AI 模型进行识图评审（需 DeepSeek Chat 识图模式）。上传图片后模型会描述图片内容并给出分析。",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "图片文件的完整路径"},
                    "context": {"type": "string", "description": "图片评审的附加上下文", "default": ""},
                },
                "required": ["image_path"],
            },
        ),
        types.Tool(
            name="screenshot",
            description="截取当前浏览器页面的截图并保存到本地。返回截图文件路径和当前页面 URL/标题。",
            inputSchema={
                "type": "object",
                "properties": {
                    "full_page": {"type": "boolean", "description": "是否截取整页（含滚动区域）", "default": False},
                    "quality": {"type": "integer", "description": "JPEG 图片质量 1-100", "default": 80},
                },
            },
        ),
        types.Tool(
            name="navigate",
            description="导航浏览器到指定 URL。可用于打开聊天页面或其他任何网页。",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要导航到的 URL"},
                    "wait_until": {"type": "string", "description": "等待条件 (domcontentloaded/load/networkidle)", "default": "domcontentloaded"},
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="click",
            description="点击页面上的元素。支持通过 CSS 选择器或文本内容匹配元素。",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS 选择器，如 #submit-btn、.btn-primary", "default": ""},
                    "text": {"type": "string", "description": "元素可见文本，如 '登录'、'发送'", "default": ""},
                    "index": {"type": "integer", "description": "同 selector 匹配多个元素时的索引", "default": 0},
                },
            },
        ),
        types.Tool(
            name="scroll",
            description="滚动当前页面。支持向上、向下滚动指定像素，或跳转到页面顶部/底部。",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "description": "滚动方向 (up/down/top/bottom)", "enum": ["up", "down", "top", "bottom"], "default": "down"},
                    "amount": {"type": "integer", "description": "滚动像素数（仅 up/down 有效）", "default": 300},
                },
            },
        ),
        types.Tool(
            name="read_page",
            description="提取当前浏览器页面的可见文本。可指定 CSS 选择器只提取特定区域。返回文本、页面 URL、标题和滚动信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS 选择器，只提取该元素的文本。不传则提取整页", "default": ""},
                },
            },
        ),
        types.Tool(
            name="type_text",
            description="在页面指定元素中输入文本。支持人类化逐字打字（短文本）和粘贴（长文本）两种方式。",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "目标元素的 CSS 选择器"},
                    "text": {"type": "string", "description": "要输入的文本内容"},
                    "human_like": {"type": "boolean", "description": "是否使用人类化输入模拟", "default": True},
                },
                "required": ["selector", "text"],
            },
        ),
        types.Tool(
            name="execute_js",
            description="在浏览器页面中执行任意 JavaScript 代码并返回结果。适用于高级浏览器自动化操作。",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 JavaScript 代码"},
                },
                "required": ["code"],
            },
        ),
        types.Tool(
            name="daemon_status",
            description="查看 daemon 的运行状态，包括当前连接的站点、浏览器存活状态和运行时长。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="clear_cache",
            description="清空评审缓存。用于测试或需要重新评审相同内容时。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="query_element",
            description="在地图上用自然语言查询页面元素坐标。输入如 'button, 发送' 'textbox, 消息' 'switch, 深度思考'，返回目标元素的坐标、角色、名称等信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "目标描述，格式: 'role, name'。如 'button, 发送' 'textbox, 消息' 'switch, 深度思考'"
                    },
                },
                "required": ["description"],
            },
        ),
    ]


async def _ensure_daemon_browser() -> dict:
    """确保 daemon 浏览器可用，否则返回错误信息。"""
    from daemon import _daemon_pool
    if _daemon_pool is None:
        return {"error": "浏览器未就绪。请先通过 CLI 运行 `python server.py --serve` 或 `python server.py --init` 初始化浏览器"}
    return {}


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "send_message":
            result = daemon_send_message(arguments["message"])
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "review_text":
            content = arguments["content"]
            context = arguments.get("context", "")
            mode = arguments.get("mode", "expert")
            cache_bypass = arguments.get("cache_bypass", False)

            # 缓存查询
            if not cache_bypass:
                key = cache_module.cache_key(content, context, mode)
                _cache = cache_module.get_cache()
                if key in _cache:
                    cached = _cache[key]
                    cached["_source"] = "cache"
                    return [types.TextContent(type="text", text=json.dumps(cached, ensure_ascii=False, indent=2))]

            result = daemon_do_review(content, context=context, deep_think=True, mode=mode)

            if "error" not in result:
                key = cache_module.cache_key(content, context, mode)
                cache_module.set_cache(key, result)
                result["_source"] = "fresh"
            else:
                result["_source"] = "error"

            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "review_image":
            image_path = arguments["image_path"]
            context = arguments.get("context", "")
            if not Path(image_path).exists():
                return [types.TextContent(
                    type="text",
                    text=json.dumps({"error": f"图片文件不存在: {image_path}"}, ensure_ascii=False, indent=2),
                )]
            result = daemon_review_image(image_path, context=context)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "screenshot":
            full_page = arguments.get("full_page", False)
            quality = arguments.get("quality", 80)
            result = daemon_screenshot(full_page=full_page, quality=quality)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "navigate":
            url = arguments["url"]
            wait_until = arguments.get("wait_until", "domcontentloaded")
            result = daemon_navigate(url, wait_until=wait_until)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "click":
            selector = arguments.get("selector", "")
            text = arguments.get("text", "")
            index = arguments.get("index", 0)
            if not selector and not text:
                return [types.TextContent(
                    type="text",
                    text=json.dumps({"error": "需要 selector 或 text 参数"}, ensure_ascii=False, indent=2),
                )]
            result = daemon_click(selector=selector or None, text=text or None, index=index)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "scroll":
            direction = arguments.get("direction", "down")
            amount = arguments.get("amount", 300)
            result = daemon_scroll(direction=direction, amount=amount)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "read_page":
            selector = arguments.get("selector", None)
            result = daemon_read(selector=selector)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "type_text":
            selector = arguments["selector"]
            text = arguments["text"]
            human_like = arguments.get("human_like", True)
            result = daemon_type_text(selector, text, human_like=human_like)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "execute_js":
            code = arguments["code"]
            result = daemon_execute(code)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "daemon_status":
            from daemon import _daemon_session, _daemon_config, _daemon_start_time, _daemon_pool
            pool_healthy = _daemon_pool is not None
            browser_alive = False
            if pool_healthy:
                try:
                    inst = _daemon_pool.instances[0] if _daemon_pool.instances else None
                    browser_alive = inst is not None and inst.healthy
                except Exception:
                    browser_alive = False
            status = {
                "status": "running" if pool_healthy else "inactive",
                "site": _daemon_config.get("name", "unknown") if _daemon_config else "unknown",
                "url": _daemon_session.get("url", "") if _daemon_session else "",
                "uptime": time.time() - _daemon_start_time if _daemon_start_time > 0 else 0,
                "browser_alive": browser_alive,
                "pool_mode": pool_healthy,
                "cache_size": cache_module.get_cache_size(),
                "cache_stats": cache_module.get_cache_stats(),
            }
            return [types.TextContent(type="text", text=json.dumps(status, ensure_ascii=False, indent=2))]

        elif name == "clear_cache":
            cache_module.clear_cache()
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "ok", "message": "所有缓存已清除"}, ensure_ascii=False, indent=2),
            )]

        elif name == "query_element":
            description = arguments["description"]
            from browser_pool import get_pool
            pool = get_pool()
            if pool is None:
                return [types.TextContent(type="text", text=json.dumps(
                    {"error": "浏览器池未启用（需要 --cdp --pool-size > 0）"}, ensure_ascii=False))]
            inst = pool.acquire(timeout=30)
            if inst is None or inst.page_map is None:
                return [types.TextContent(type="text", text=json.dumps(
                    {"error": "地图未就绪"}, ensure_ascii=False))]
            try:
                from page_map import quick_find
                results = quick_find(inst.page_map, description)
                return [types.TextContent(type="text", text=json.dumps(
                    [r.to_dict() for r in results[:10]], ensure_ascii=False, indent=2))]
            finally:
                pool.release(inst)

        else:
            return [types.TextContent(
                type="text",
                text=json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False, indent=2),
            )]

    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        sys.stderr.write(f"[MCP Error] {name}({arguments}): {e}\n{error_detail}\n")
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": str(e), "detail": error_detail}, ensure_ascii=False, indent=2),
        )]


# ═══════════════════════════════════════════════════════════════
# MCP Server 启动
# ═══════════════════════════════════════════════════════════════

async def run_mcp_server():
    """启动 MCP stdio Server。"""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="web-chat-bridge",
                server_version="4.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities=None,
                ),
            ),
        )


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="网页大模型对话桥接 v4 — MCP Server")
    parser.add_argument("--init", action="store_true", help="初始化浏览器会话")
    parser.add_argument("--url", default="https://chat.deepseek.com", help="聊天页面 URL")
    parser.add_argument("--site", help="站点标识 (deepseek/openai/claude/kimi/tongyi/doubao)")

    # Actor-Critic 评审
    parser.add_argument("--review", help="评审一段文本")
    parser.add_argument("--review-file", help="从文件读取产出并评审")
    parser.add_argument("--review-image", help="上传图片到识图模式并评审")
    parser.add_argument("--context", default="", help="评审附加上下文说明")
    parser.add_argument("--no-cache", action="store_true", help="跳过评审缓存，强制重新请求")
    parser.add_argument("--mode", default="expert", choices=["fast", "expert", "vision"], help="模式切换")

    # CDP
    parser.add_argument("--cdp", action="store_true", help="通过 CDP 连接已有浏览器")
    parser.add_argument("--cdp-port", type=int, default=9222, help="CDP 调试端口 (默认 9222)")
    parser.add_argument("--cdp-auto-launch", action="store_true", default=True,
                        help="自动启动 Edge（默认 true），--no-cdp-auto-launch 禁用")
    parser.add_argument("--cdp-browser", default=None,
                        help="指定浏览器路径，如 chrome.exe 或 msedge.exe 的完整路径")

    # v4.1 路由 + 浏览器池 + 打字策略
    parser.add_argument("--route", default="auto", choices=["auto", "api", "browser"],
                        help="路由模式: auto=智能路由, api=强制API, browser=强制浏览器 (默认 auto)")
    parser.add_argument("--pool-size", type=int, default=0,
                        help="浏览器池大小 (默认 0=禁用池，使用单实例; >0 如 2 则启用池子)")
    parser.add_argument("--type-strategy", default=None, choices=["human", "fast", "instant"],
                        help="打字策略: human=人类模拟, fast=快速, instant=即时 (默认 human)")
    parser.add_argument("--review-async", action="store_true", default=True,
                        help="启用异步评审 (默认 true)")

    # 通用
    parser.add_argument("--send", help="发送单条消息")
    parser.add_argument("--direct", help="直连模式：发送消息后立即退出（不启动 daemon）")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP daemon（与 MCP 并存）")
    parser.add_argument("--port", type=int, default=DAEMON_PORT, help=f"Daemon 端口 (默认 {DAEMON_PORT})")
    parser.add_argument("--cache-info", action="store_true", help="显示缓存统计信息后退出")

    args = parser.parse_args()

    # 加载持久化缓存
    cache_module.load_cache()
    atexit.register(cache_module.flush_cache)

    # --cache-info
    if args.cache_info:
        stats = cache_module.get_cache_stats()
        total = stats["total"]
        hit_rate = f"{(stats['hit_l1'] + stats['hit_l2']) / total * 100:.1f}%" if total else "N/A"
        print(json.dumps({
            "cache_size": cache_module.get_cache_size(),
            "cache_maxsize": __import__('config').CACHE_MAXSIZE,
            "cache_ttl_seconds": __import__('config').CACHE_TTL,
            "cache_file": str(CACHE_FILE),
            "stats": stats,
            "hit_rate": hit_rate,
        }, ensure_ascii=False, indent=2))
        return

    # --init: 初始化浏览器
    if args.init:
        site_id = args.site
        if site_id is None:
            from chat_adapters import get_site_config
            site_id, _ = get_site_config(args.url)
        init_browser(args.url, site_id)
        return

    # --direct: 直连模式下发送一条消息后立即退出
    if args.direct:
        from browser_pool import BrowserPool
        from browser_controller import send_message as direct_send
        site_id = args.site or "deepseek"
        pool = BrowserPool(size=1, site=site_id)
        pool.start()
        inst = pool.acquire(timeout=30)
        if inst is None:
            print(json.dumps({"error": "浏览器未就绪"}, ensure_ascii=False))
            return
        config = SITE_CONFIGS.get(inst.site_id, SITE_CONFIGS["custom"])
        result = direct_send(inst.page, config, args.direct)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        pool.release(inst)
        return

    # --serve: HTTP daemon（后台运行，保持进程存活）
    if args.serve:
        # P2-2: --route api 跳过浏览器初始化
        if args.route == "api":
            import config as _cfg
            _cfg.ROUTE_MODE = "api"
            sys.stderr.write(f"[Router] API 模式，跳过浏览器初始化\n")
            # 启动薄 HTTP daemon（无浏览器池）
            import threading
            daemon_thread = threading.Thread(
                target=start_daemon,
                args=(args.port, args.site, args.url),
                kwargs={
                    "cdp": False,
                    "cdp_port": args.cdp_port,
                    "cdp_auto_launch": False,
                    "cdp_browser_path": None,
                    "pool_size": 0,
                    "type_strategy": args.type_strategy,
                    "route_mode": "api",
                },
                daemon=True,
            )
            daemon_thread.start()
            print(f"[Daemon] HTTP 服务已在端口 {args.port} 启动（后台线程，API 模式）", flush=True)
            print(f"[Daemon] 按 Ctrl+C 停止服务", flush=True)
            try:
                while True:
                    time.sleep(10)
            except KeyboardInterrupt:
                print("\n[Daemon] 收到停止信号", flush=True)
            return

        # 应用路由配置
        if args.route != "auto":
            import config as _cfg
            _cfg.ROUTE_MODE = args.route
            sys.stderr.write(f"[Router] 路由模式已设置: {args.route}\n")
        
        # 应用打字策略
        if args.type_strategy:
            from daemon import set_type_strategy
            set_type_strategy(args.type_strategy)
        
        # 启动 HTTP daemon 在后台线程
        import threading
        daemon_thread = threading.Thread(
            target=start_daemon,
            args=(args.port, args.site, args.url),
            kwargs={
                "cdp": args.cdp,
                "cdp_port": args.cdp_port,
                "cdp_auto_launch": args.cdp_auto_launch,
                "cdp_browser_path": args.cdp_browser,
                "pool_size": args.pool_size,
                "type_strategy": args.type_strategy,
                "route_mode": args.route,
            },
            daemon=True,
        )
        daemon_thread.start()
        print(f"[Daemon] HTTP 服务已在端口 {args.port} 启动（后台线程）", flush=True)
        print(f"[Daemon] 路由: {args.route} | 池大小: {args.pool_size or '单实例'} | 打字策略: {args.type_strategy or 'human'}", flush=True)
        print(f"[Daemon] 按 Ctrl+C 停止服务", flush=True)
        # 保持主线程存活
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            print("\n[Daemon] 收到停止信号", flush=True)
        return

    # 路由决策日志（不是 serve 模式时）
    if args.route and args.route != "auto":
        sys.stderr.write(f"[Router] 路由模式: {args.route}\n")
    
    # 打印异步评审状态
    if args.review_async:
        sys.stderr.write(f"[Review] 异步评审已启用\n")

    # --review 或 --review-file
    if args.review or args.review_file:
        content = args.review or ""
        if args.review_file:
            try:
                content = Path(args.review_file).read_text(encoding="utf-8")
                sys.stderr.write(f"[Actor] 读取文件: {args.review_file} ({len(content)} 字符)\n")
            except Exception as e:
                print(json.dumps({"error": f"无法读取文件: {e}"}, ensure_ascii=False))
                return

        # 优先通过 daemon
        ok, result = _call_daemon("review", {
            "content": content,
            "context": args.context,
            "deep_think": True,
            "mode": args.mode,
            "cache_bypass": args.no_cache,
        }, port=args.port)
        if ok:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write(f"[Fallback] {result.get('error', 'daemon 不可用')}，使用直接模式\n")
            result = do_review(content, args.context, deep_think=True, mode=args.mode, site=args.site, url=args.url if args.url != "https://chat.deepseek.com" else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # --review-image
    if args.review_image:
        image_path = args.review_image
        if not Path(image_path).exists():
            print(json.dumps({"error": f"图片不存在: {image_path}"}, ensure_ascii=False))
            return
        ok, result = _call_daemon("review-image", {
            "image": str(Path(image_path).resolve()),
            "context": args.context,
            "mode": "vision",
        }, port=args.port)
        if ok:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write(f"[Fallback] {result.get('error', 'daemon 不可用')}，图片评审需要 daemon 模式\n")
        return

    # --send
    if args.send:
        ok, result = _call_daemon("send", {"message": args.send}, port=args.port)
        if ok:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write(f"[Fallback] {result.get('error', 'daemon 不可用')}，使用直接模式\n")
            single_send(args.send)
        return

    # 默认：启动 MCP stdio server
    import asyncio
    asyncio.run(run_mcp_server())


if __name__ == "__main__":
    main()