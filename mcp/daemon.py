"""
web-chat-bridge MCP — Daemon 管理

向后兼容的 HTTP server（可选），与 MCP Server 并存时使用。
提供与 v3 相同的 HTTP API 端点。

v4.2 重构：
- 所有请求走浏览器池（BrowserPool），无单实例 fallback
- 移除 greenlet 跨线程冲突根源
- 移除 _daemon_page/_daemon_browser 全局单例
"""

import http.server
import json
import random
import socketserver
import sys
import threading
import time
from pathlib import Path

from browser_controller import (
    send_message, _try_recover_page, enable_deep_think, switch_mode,
    init_browser, connect_via_cdp, launch_edge_with_cdp, wait_for_cdp,
)
from chat_adapters import get_site_config
from config import (
    DAEMON_PORT, SESSION_FILE, SITE_CONFIGS, SCREENSHOT_DIR, POOL_SIZE,
    TYPE_STRATEGY, TYPE_FAST_DELAY,
    HUMAN_MIN_DELAY, HUMAN_MAX_DELAY,
    HUMAN_PAUSE_MIN, HUMAN_PAUSE_MAX,
    HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX,
    PUNCTUATION_PAUSE, PASTE_THRESHOLD,
)
from image_handler import _send_with_image, _wait_for_image_preview
from review_engine import parse_review_json, CRITIC_PROMPT
from cache import cache_key, get_cache, get_cache_stats, get_cache_size, clear_cache as cache_clear
from page_map import quick_find

import cache as cache_module


# ── 全局 daemon 状态 ──
_daemon_config = None
_daemon_session = None
_daemon_start_time = 0.0
_daemon_pool = None          # BrowserPool 引用
_daemon_type_strategy = TYPE_STRATEGY  # human / fast / instant


# ── 打字策略 ──

def _human_type(page, input_el, text: str):
    """模拟人类打字：可变延迟、段落间歇、标点停顿。"""
    try:
        input_el.click()
        time.sleep(random.uniform(0.2, 0.5))
    except Exception:
        pass

    total = len(text)
    next_pause_at = random.randint(HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX)
    
    for i, char in enumerate(text):
        try:
            page.keyboard.press(char)
        except Exception:
            try:
                page.keyboard.insert_text(char)
            except Exception:
                pass

        delay = random.uniform(HUMAN_MIN_DELAY, HUMAN_MAX_DELAY)
        if char in '，。！？；：、—…""''）》》《' or char in ',.!?;:':
            delay += PUNCTUATION_PAUSE
        if i >= next_pause_at:
            time.sleep(random.uniform(HUMAN_PAUSE_MIN, HUMAN_PAUSE_MAX))
            next_pause_at = i + random.randint(HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX)
        time.sleep(delay)

    time.sleep(random.uniform(0.3, 0.8))
    sys.stderr.write(f"[输入] {total} 字符逐字输入完成\n")


def _fast_type(page, input_el, text: str):
    """快速模式：固定短延迟，无随机停顿。"""
    try:
        input_el.click()
        time.sleep(0.1)
    except Exception:
        pass

    for char in text:
        try:
            page.keyboard.press(char)
        except Exception:
            try:
                page.keyboard.insert_text(char)
            except Exception:
                pass
        time.sleep(TYPE_FAST_DELAY)
    
    sys.stderr.write(f"[输入] {len(text)} 字符快速输入完成\n")


def _instant_input(page, input_el, text: str):
    """即时模式：直接 fill 或粘贴，<1s。"""
    try:
        input_el.click()
        time.sleep(0.05)
    except Exception:
        pass
    try:
        input_el.fill(text)
    except Exception:
        try:
            page.keyboard.insert_text(text)
        except Exception:
            pass
    sys.stderr.write(f"[输入] {len(text)} 字符即时输入完成\n")


def _type_text_strategic(page, input_el, text: str, config: dict):
    """根据打字策略选择输入方式。"""
    strategy = _daemon_type_strategy or TYPE_STRATEGY
    
    if strategy == "instant":
        _instant_input(page, input_el, text)
    elif strategy == "fast":
        _fast_type(page, input_el, text)
    else:
        # human 模式：短文本逐字，长文本粘贴
        if len(text) < PASTE_THRESHOLD:
            _human_type(page, input_el, text)
        else:
            from browser_controller import human_paste
            human_paste(page, input_el, config, text)


# ── 池模式函数（v4.2: 所有请求走池子，无单实例 fallback）──

def daemon_send_message(message: str) -> dict:
    """通过浏览器池发送消息。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=60)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        from chat_adapters import find_and_fill_input
        input_el = find_and_fill_input(inst.page, _daemon_config)
        if input_el:
            _type_text_strategic(inst.page, input_el, message, _daemon_config)
        return send_message(inst.page, _daemon_config, message)
    finally:
        pool.release(inst)


def daemon_do_review(content: str, context: str = "", deep_think: bool = True,
                     mode: str = "expert") -> dict:
    """通过浏览器池执行评审。"""
    global _daemon_session, _daemon_config
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=120)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        site_id = _daemon_session.get("site_id", "custom") if _daemon_session else "deepseek"
        from config import MAX_CONTENT_CHARS

        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + f"\n\n... (截断，原长度 {len(content)} 字符)"

        prompt = CRITIC_PROMPT
        if context:
            prompt = prompt + f"\n\n上下文：{context}\n"

        full_message = prompt + content
        sys.stderr.write(f"[Critic] 发送 {len(full_message)} 字符，等待评审...\n")

        try:
            if site_id == "deepseek":
                switch_mode(inst.page, mode)
                if deep_think:
                    enable_deep_think(inst.page)
        except Exception:
            pass

        result = send_message(inst.page, _daemon_config, full_message)

        if "error" in result:
            return result

        review = parse_review_json(result["response"])
        review["raw"] = result["response"]
        review["_meta"] = {
            "site": _daemon_config["name"],
            "content_chars": len(content),
            "sent_chars": len(full_message),
            "timestamp": result.get("timestamp"),
        }
        return review
    finally:
        pool.release(inst)


def daemon_review_image(image_path: str, context: str = "", mode: str = "vision") -> dict:
    """通过浏览器池上传图片并评审。"""
    global _daemon_config
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=60)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        try:
            switch_mode(inst.page, mode)
        except Exception:
            pass

        full_prompt = "请仔细查看这张图片，描述你看到的所有内容。如果图片包含文本、数据、图表、错误信息等，请逐一解读。"
        if context:
            full_prompt = f"{context}\n\n{full_prompt}"

        sys.stderr.write(f"[识图] 上传图片: {image_path}\n")
        result = _send_with_image(inst.page, _daemon_config, image_path, full_prompt)

        if "error" in result:
            return result

        review = parse_review_json(result["response"])
        review["raw"] = result["response"]
        review["_meta"] = {
            "site": _daemon_config["name"],
            "image": image_path,
            "timestamp": result.get("timestamp"),
        }
        return review
    finally:
        pool.release(inst)


# ── Browser Agent 操作函数 ──

def daemon_screenshot(full_page: bool = False, quality: int = 80) -> dict:
    """截图并保存到本地。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"shot_{ts}.jpg"
        filepath = SCREENSHOT_DIR / filename
        inst.page.screenshot(path=str(filepath), full_page=full_page, quality=quality, type="jpeg")
        sys.stderr.write(f"[Agent] 截图已保存: {filepath}\n")
        return {
            "status": "ok",
            "path": str(filepath),
            "filename": filename,
            "url": inst.page.url,
            "title": inst.page.title(),
        }
    except Exception as e:
        return {"error": f"截图失败: {e}"}
    finally:
        pool.release(inst)


def daemon_navigate(url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> dict:
    """导航到指定 URL。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        inst.page.goto(url, wait_until=wait_until, timeout=timeout)
        sys.stderr.write(f"[Agent] 导航到: {url}\n")
        return {"status": "ok", "url": inst.page.url, "title": inst.page.title()}
    except Exception as e:
        return {"error": f"导航失败: {e}"}
    finally:
        pool.release(inst)


def daemon_click(selector: str = None, text: str = None, index: int = 0, timeout: int = 5000) -> dict:
    """点击元素：支持 CSS selector 或文本内容匹配。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        if selector:
            el = inst.page.locator(selector).nth(index)
        elif text:
            el = inst.page.get_by_text(text, exact=False).first
        else:
            return {"error": "需要 selector 或 text 参数"}
        if not el.is_visible(timeout=timeout):
            return {"error": f"元素不可见: {selector or text}"}
        tag = el.evaluate("el => el.tagName")
        el_text = el.inner_text()[:80] if el else ""
        el.click()
        sys.stderr.write(f"[Agent] 点击: <{tag}> {el_text}\n")
        time.sleep(0.5)
        return {"status": "ok", "clicked": f"<{tag}> {el_text}", "url": inst.page.url, "title": inst.page.title()}
    except Exception as e:
        return {"error": f"点击失败: {e}"}
    finally:
        pool.release(inst)


def daemon_scroll(direction: str = "down", amount: int = 300) -> dict:
    """滚动页面。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        if direction == "down":
            inst.page.evaluate(f"window.scrollBy(0, {amount})")
        elif direction == "up":
            inst.page.evaluate(f"window.scrollBy(0, -{amount})")
        elif direction == "bottom":
            inst.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top":
            inst.page.evaluate("window.scrollTo(0, 0)")
        else:
            return {"error": f"不支持的方向: {direction} (支持: up/down/top/bottom)"}
        scroll_y = inst.page.evaluate("window.scrollY")
        return {"status": "ok", "direction": direction, "scrollY": scroll_y}
    except Exception as e:
        return {"error": f"滚动失败: {e}"}
    finally:
        pool.release(inst)


def daemon_read(selector: str = None) -> dict:
    """提取页面可见文本。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        if selector:
            el = inst.page.locator(selector).first
            if not el.is_visible():
                return {"error": f"元素不可见: {selector}"}
            text = el.inner_text()
            tag = el.evaluate("el => el.tagName")
        else:
            text = inst.page.locator("body").inner_text()
            tag = "body"
        max_chars = 10000
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + f"\n\n... (截断，原长度 {len(text)} 字符)"
        return {
            "status": "ok", "tag": tag, "text": text, "truncated": truncated,
            "total_chars": len(text) if not truncated else max_chars,
            "url": inst.page.url, "title": inst.page.title(),
            "scrollY": inst.page.evaluate("window.scrollY"),
            "scrollHeight": inst.page.evaluate("document.body.scrollHeight"),
        }
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    finally:
        pool.release(inst)


def daemon_type_text(selector: str, text: str, human_like: bool = True) -> dict:
    """在指定元素中输入文本。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        el = inst.page.locator(selector).first
        if not el.is_visible():
            return {"error": f"元素不可见: {selector}"}
        el.click()
        el.fill("")
        from config import PASTE_THRESHOLD
        
        strategy = _daemon_type_strategy or TYPE_STRATEGY
        if strategy == "instant":
            _instant_input(inst.page, el, text)
        elif strategy == "fast":
            _fast_type(inst.page, el, text)
        elif human_like and len(text) < PASTE_THRESHOLD:
            _human_type(inst.page, el, text)
        else:
            from browser_controller import human_paste
            human_paste(inst.page, el, _daemon_config or {}, text)
        
        return {
            "status": "ok", "selector": selector, "length": len(text),
            "method": strategy,
        }
    except Exception as e:
        return {"error": f"输入失败: {e}"}
    finally:
        pool.release(inst)


def daemon_execute(js_code: str) -> dict:
    """在页面中执行 JavaScript。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        result = inst.page.evaluate(js_code)
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"error": f"JS 执行失败: {e}"}
    finally:
        pool.release(inst)


# ── 语义直连 + 三级降级点击 ──

def daemon_find_and_click(role: str = None, name: str = None, description: str = None) -> dict:
    """
    三级降级点击：
    1. 语义直连: get_by_role(role, name=name).click()
    2. 地图坐标: query_element → mouse.click(cx, cy)
    3. 文字匹配: get_by_text(name).click()
    """
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        page = inst.page

        # Level 1: 语义直连
        if role:
            try:
                loc = page.get_by_role(role, name=name) if name else page.get_by_role(role)
                if loc.count() > 0:
                    loc.first.click()
                    return {"status": "ok", "method": "semantic", "role": role, "name": name}
            except Exception:
                pass

        # Level 2: 地图坐标
        if inst.page_map and (name or description):
            desc = description or f"{role or ''}, {name or ''}".strip(", ")
            results = quick_find(inst.page_map, desc)
            if results:
                node = results[0]
                page.mouse.click(node.cx, node.cy)
                return {"status": "ok", "method": "coordinate", "score": inst.page_map.score_node(node), "target": node.to_dict()}

        # Level 3: 文字匹配
        if name:
            try:
                loc = page.get_by_text(name, exact=False)
                if loc.count() > 0:
                    loc.first.click()
                    return {"status": "ok", "method": "text", "name": name}
            except Exception:
                pass

        return {"error": f"三级降级均未找到目标: role={role} name={name}"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        pool.release(inst)


def daemon_find_and_type(role: str = "textbox", name: str = None, text: str = "") -> dict:
    """语义直连输入：锁定输入框并填入文本。"""
    pool = _daemon_pool
    if pool is None:
        return {"error": "需要浏览器池模式（--cdp --pool-size > 0）"}
    inst = pool.acquire(timeout=30)
    if inst is None:
        return {"error": "浏览器池忙，请稍后再试"}
    try:
        page = inst.page
        if name:
            loc = page.get_by_role(role, name=name)
        else:
            loc = page.get_by_role(role)
        if loc.count() == 0:
            return {"error": f"未找到 role={role} name={name}"}
        loc.first.fill(text)
        return {"status": "ok", "role": role, "name": name, "text": text}
    except Exception as e:
        return {"error": str(e)}
    finally:
        pool.release(inst)


# ── HTTP Daemon (向后兼容) ──

def _check_daemon(port: int = None):
    """检测 daemon 是否存活。"""
    if port is None:
        port = DAEMON_PORT
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, {"error": str(e), "port": port}


def _call_daemon(endpoint: str, data: dict, port: int = None):
    """向 daemon 发送 HTTP 请求。"""
    if port is None:
        port = DAEMON_PORT
    import urllib.request
    try:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/{endpoint}",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.request.URLError:
        return False, {"error": f"无法连接 daemon (端口 {port})，请先运行 --serve"}
    except Exception as e:
        return False, {"error": str(e)}


class DaemonHandler(http.server.BaseHTTPRequestHandler):
    """处理来自 CLI 的 HTTP 请求（与 v3 向后兼容）。"""

    def _json_reply(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global _daemon_session, _daemon_config, _daemon_start_time, _daemon_pool
        if self.path == "/status":
            self._json_reply({
                "status": "ok",
                "site": _daemon_config.get("name", "unknown") if _daemon_config else "unknown",
                "url": _daemon_session.get("url", "") if _daemon_session else "",
            })
        elif self.path == "/health":
            pool_healthy = _daemon_pool is not None
            browser_alive = False
            if pool_healthy:
                try:
                    inst = _daemon_pool.instances[0] if _daemon_pool.instances else None
                    browser_alive = inst is not None and inst.healthy
                except Exception:
                    browser_alive = False
            self._json_reply({
                "status": "ok",
                "uptime": time.time() - _daemon_start_time,
                "browser_alive": browser_alive,
                "pool_mode": pool_healthy,
            })
        else:
            self._json_reply({"error": "not found"}, 404)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_reply({"error": "invalid JSON"}, 400)
            return

        if self.path == "/send":
            message = data.get("message", "")
            if not message:
                self._json_reply({"error": "缺少 message 字段"}, 400)
                return
            try:
                result = daemon_send_message(message)
                self._json_reply(result)
            except Exception as e:
                self._json_reply({"error": str(e)}, 500)

        elif self.path == "/review":
            content = data.get("content", "")
            if not content:
                self._json_reply({"error": "缺少 content 字段"}, 400)
                return
            context = data.get("context", "")
            mode = data.get("mode", "expert")
            bypass = data.get("cache_bypass", False)

            if not bypass:
                key = cache_key(content, context, mode)
                _cache = get_cache()
                if key in _cache:
                    cached = _cache[key]
                    cached["_source"] = "cache"
                    self._json_reply(cached)
                    return

            result = daemon_do_review(
                content, context=context,
                deep_think=data.get("deep_think", True),
                mode=mode,
            )

            if "error" not in result:
                key = cache_key(content, context, mode)
                cache_module.set_cache(key, result)
                result["_source"] = "fresh"
            else:
                result["_source"] = "error"

            self._json_reply(result)

        elif self.path == "/review-image":
            image = data.get("image", "")
            if not image:
                self._json_reply({"error": "缺少 image 字段"}, 400)
                return
            result = daemon_review_image(
                image,
                context=data.get("context", ""),
                mode=data.get("mode", "vision"),
            )
            self._json_reply(result)

        elif self.path == "/shutdown":
            self._json_reply({"status": "shutting down"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        elif self.path == "/cache/clear":
            scope = data.get("scope", "all")
            if scope == "all":
                cache_clear()
                self._json_reply({"status": "ok", "cleared": "all", "note": "所有缓存已清除"})
            else:
                self._json_reply({"error": f"不支持的 scope: {scope}"}, 400)

        # ── Browser Agent 端点 ──
        elif self.path == "/screenshot":
            self._json_reply(daemon_screenshot(
                full_page=data.get("full_page", False),
                quality=data.get("quality", 80),
            ))
        elif self.path == "/navigate":
            url = data.get("url", "")
            if not url:
                self._json_reply({"error": "缺少 url 字段"}, 400)
                return
            self._json_reply(daemon_navigate(
                url,
                wait_until=data.get("wait_until", "domcontentloaded"),
                timeout=data.get("timeout", 30000),
            ))
        elif self.path == "/click":
            if not data.get("selector") and not data.get("text"):
                self._json_reply({"error": "需要 selector 或 text 参数"}, 400)
                return
            self._json_reply(daemon_click(
                selector=data.get("selector"),
                text=data.get("text"),
                index=data.get("index", 0),
                timeout=data.get("timeout", 5000),
            ))
        elif self.path == "/scroll":
            self._json_reply(daemon_scroll(
                direction=data.get("direction", "down"),
                amount=data.get("amount", 300),
            ))
        elif self.path == "/read":
            self._json_reply(daemon_read(selector=data.get("selector")))
        elif self.path == "/type":
            selector = data.get("selector", "")
            text = data.get("text", "")
            if not selector or not text:
                self._json_reply({"error": "需要 selector 和 text 参数"}, 400)
                return
            self._json_reply(daemon_type_text(
                selector, text,
                human_like=data.get("human_like", True),
            ))
        elif self.path == "/execute":
            js_code = data.get("code", "")
            if not js_code:
                self._json_reply({"error": "缺少 code 字段"}, 400)
                return
            self._json_reply(daemon_execute(js_code))
        else:
            self._json_reply({"error": "not found"}, 404)

    def log_message(self, format, *args):
        sys.stderr.write(f"[Daemon] {args[0]}\n")


def set_type_strategy(strategy: str):
    """设置全局打字策略。"""
    global _daemon_type_strategy
    if strategy in ("human", "fast", "instant"):
        _daemon_type_strategy = strategy
        sys.stderr.write(f"[Daemon] 打字策略已切换: {strategy}\n")
    else:
        sys.stderr.write(f"[Daemon] 不支持的打字策略: {strategy} (使用 human/fast/instant)\n")


def start_daemon(port: int = DAEMON_PORT, site: str = None, url: str = None,
                 cdp: bool = False, cdp_port: int = 9222,
                 cdp_auto_launch: bool = True, cdp_browser_path: str = None,
                 pool_size: int = 0, type_strategy: str = None,
                 route_mode: str = None):
    """启动 HTTP daemon，v4.2 纯池模式。
    
    Args:
        port: HTTP daemon 监听端口
        site: 站点标识
        url: 聊天页面 URL
        cdp: 是否通过 CDP 连接已有浏览器
        cdp_port: CDP 调试端口
        cdp_auto_launch: 是否自动启动浏览器
        cdp_browser_path: 自定义浏览器路径
        pool_size: 浏览器池大小（0=使用默认 POOL_SIZE）
        type_strategy: 打字策略
        route_mode: 路由模式
    """
    global _daemon_config, _daemon_session, _daemon_start_time, _daemon_pool, _daemon_type_strategy

    _daemon_start_time = time.time()
    
    # 设置打字策略
    if type_strategy:
        _daemon_type_strategy = type_strategy
    
    # 设置路由模式
    if route_mode:
        import config as _config_module
        _config_module.ROUTE_MODE = route_mode
        sys.stderr.write(f"[Daemon] 路由模式已设置: {route_mode}\n")

    # 端口冲突检测
    alive, info = _check_daemon(port)
    if alive:
        browser_ok = info.get("browser_alive", False)
        uptime = info.get("uptime", -1)
        if browser_ok:
            print(f"[Daemon] 端口 {port} 已有运行中的 daemon (浏览器正常, 已运行 {uptime:.0f}s)", flush=True)
            print(f"[Daemon] 无需重复启动，直接使用现有 daemon 即可", flush=True)
            return
        else:
            sys.stderr.write(f"[Daemon] 端口 {port} 上有残留 daemon (浏览器已死), 请先终止该进程\n")
            return

    # SESSION_FILE 不存在时自动 init
    if not SESSION_FILE.exists():
        sys.stderr.write(f"[Daemon] SESSION_FILE 不存在，自动初始化浏览器...\n")
        site_id = site or "deepseek"
        init_url = url or SITE_CONFIGS.get(site_id, {}).get("url", "https://chat.deepseek.com")
        init_browser(init_url, site_id)
        if not SESSION_FILE.exists():
            print(json.dumps({"error": "浏览器初始化失败，SESSION_FILE 仍未创建"}, ensure_ascii=False))
            return
        sys.stderr.write(f"[Daemon] 浏览器初始化完成，继续启动 daemon...\n")

    _daemon_session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    
    # CDP 模式下标记 session
    if cdp:
        _daemon_session["cdp"] = True
        _daemon_session["cdp_port"] = cdp_port
        _daemon_session["cdp_auto_launch"] = cdp_auto_launch
        _daemon_session["cdp_browser_path"] = cdp_browser_path
        SESSION_FILE.write_text(json.dumps(_daemon_session, ensure_ascii=False, indent=2))

    if site and site in SITE_CONFIGS and site != "custom":
        _daemon_session["site_id"] = site
        _daemon_session["url"] = SITE_CONFIGS[site].get("url", _daemon_session["url"])
    elif url:
        site_id, _ = get_site_config(url)
        _daemon_session["site_id"] = site_id
        _daemon_session["url"] = url

    site_id = _daemon_session.get("site_id", "custom")
    _daemon_config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])

    # ── 浏览器池模式（v4.2: 强制池模式，API 路由除外）──
    if route_mode == "api":
        sys.stderr.write(f"[Daemon] API 模式，跳过浏览器池初始化\n")
        _daemon_pool = None
    else:
        effective_pool_size = pool_size if pool_size > 0 else POOL_SIZE
        sys.stderr.write(f"[Daemon] 强制浏览器池模式 (size={effective_pool_size}, site={site_id})\n")
        
        from browser_pool import BrowserPool, set_pool
        _pool = BrowserPool(size=effective_pool_size, site=site_id)
        _pool.start()
        set_pool(_pool)
        _daemon_pool = _pool

    # 启动 HTTP 服务
    server = http.server.HTTPServer(("127.0.0.1", port), DaemonHandler)
    print(f"[Daemon] HTTP 服务: http://127.0.0.1:{port}", flush=True)
    print(f"[Daemon] Actor-Critic: POST /send | /review | /review-image | /cache/clear", flush=True)
    print(f"[Daemon] Browser Agent: POST /navigate | /screenshot | /read | /click | /scroll | /type | /execute", flush=True)
    print(f"[Daemon] 管理: GET /status | /health | POST /shutdown", flush=True)
    print(f"[Daemon] 浏览器池: {len(_daemon_pool.instances)} 实例 | 策略: {_daemon_type_strategy}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Daemon] 正在关闭...", flush=True)
    finally:
        server.server_close()
        try:
            if _daemon_pool:
                for inst in _daemon_pool.instances:
                    try:
                        if inst.playwright:
                            inst.playwright.stop()
                    except Exception:
                        pass
        except Exception:
            pass