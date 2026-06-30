"""
web-chat-bridge MCP — Daemon 管理

向后兼容的 HTTP server（可选），与 MCP Server 并存时使用。
提供与 v3 相同的 HTTP API 端点。

v4.1 重构：
- 浏览器常驻池（BrowserPool）：预启动 N 个实例，任务取用后归还
- 打字策略分层：human / fast / instant
- 旧 daemon 单实例模式保持向后兼容
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
    DAEMON_PORT, SESSION_FILE, SITE_CONFIGS, SCREENSHOT_DIR,
    TYPE_STRATEGY, TYPE_FAST_DELAY,
    HUMAN_MIN_DELAY, HUMAN_MAX_DELAY,
    HUMAN_PAUSE_MIN, HUMAN_PAUSE_MAX,
    HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX,
    PUNCTUATION_PAUSE, PASTE_THRESHOLD,
)
from image_handler import _send_with_image, _wait_for_image_preview
from review_engine import parse_review_json, CRITIC_PROMPT
from cache import cache_key, get_cache, get_cache_stats, get_cache_size, clear_cache as cache_clear

import cache as cache_module


# ── 全局 daemon 状态 ──
_daemon_browser = None
_daemon_page = None
_daemon_config = None
_daemon_session = None
_daemon_playwright = None
_daemon_start_time = 0.0
_daemon_lock = threading.Lock()
_daemon_heartbeat_stop = threading.Event()
_daemon_pool = None          # BrowserPool 引用（启用池子时设置）
_daemon_type_strategy = TYPE_STRATEGY  # human / fast / instant


def _with_lock(timeout: int = 60):
    """返回一个上下文管理器，获取全局锁。超时返回错误 dict。"""
    acquired = _daemon_lock.acquire(timeout=timeout)
    if not acquired:
        return {"error": "浏览器正忙，请稍后再试"}
    return None


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


# ── CDP 重连逻辑 ──

def _reconnect_cdp():
    """自动重连 CDP 浏览器（launch + wait + connect，最多 3 次）。"""
    global _daemon_playwright, _daemon_browser, _daemon_page
    port = _daemon_session.get("cdp_port", 9222)
    browser_path = _daemon_session.get("cdp_browser_path", None)

    for attempt in range(3):
        sys.stderr.write(f"[Daemon] CDP 重连尝试 {attempt+1}/3 (端口 {port})...\n")
        try:
            # 等待或启动浏览器
            if not wait_for_cdp(port, timeout=10):
                launch_edge_with_cdp(port, browser_path=browser_path)
                wait_for_cdp(port, timeout=30)

            # 销毁旧连接
            try:
                if _daemon_playwright:
                    _daemon_playwright.stop()
            except Exception:
                pass
            _daemon_playwright = None
            _daemon_browser = None
            _daemon_page = None

            _daemon_playwright, _daemon_browser, _daemon_page = connect_via_cdp(port)
            site_id = _daemon_session.get("site_id", "custom")
            if site_id == "deepseek":
                try:
                    switch_mode(_daemon_page, "expert")
                    enable_deep_think(_daemon_page)
                except Exception:
                    pass
            sys.stderr.write(f"[Daemon] CDP 已重连 (端口 {port})\n")
            return True
        except Exception as e:
            sys.stderr.write(f"[Daemon] CDP 重连尝试 {attempt+1}/3 失败: {e}\n")
            time.sleep(3)
    return False


# ── 心跳线程 ──

def _heartbeat_loop():
    """后台心跳线程：每 15 秒检测 CDP 连接是否存活 + URL 漂移检测。"""
    while not _daemon_heartbeat_stop.is_set():
        time.sleep(15)
        try:
            page = _daemon_page
            session = _daemon_session
            if page is None:
                sys.stderr.write("[Heartbeat] CDP 连接断开 (page is None)，尝试重连...\n")
            elif page.is_closed():
                sys.stderr.write("[Heartbeat] CDP 连接断开 (page closed)，尝试重连...\n")
            else:
                # URL 漂移检测
                target_url = session.get("url", "") if session else ""
                if target_url:
                    current_url = page.url
                    if target_url not in current_url:
                        sys.stderr.write(f"[Heartbeat] URL 漂移: {current_url} → 重新导航到 {target_url}\n")
                        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(2)
                        sys.stderr.write(f"[Heartbeat] 导航恢复: {page.url}\n")
                        continue
                
                # 轻量心跳：evaluate 一个简单 JS
                page.evaluate("1 + 1")
                continue  # 正常，继续等待
        except Exception:
            sys.stderr.write("[Heartbeat] 心跳失败，尝试重连...\n")

        # 重连
        try:
            _reconnect_cdp()
        except Exception as e:
            sys.stderr.write(f"[Heartbeat] 重连失败: {e}\n")


def _try_recover_page_wrapper():
    """包装 _try_recover_page 以匹配全局变量签名。"""
    global _daemon_browser, _daemon_page, _daemon_session, _daemon_playwright
    ok, _daemon_browser, _daemon_page, _daemon_playwright = _try_recover_page(
        _daemon_browser, _daemon_page, _daemon_session, _daemon_playwright,
    )
    return ok


def _ensure_page():
    """确保浏览器页面可用，否则尝试恢复。"""
    global _daemon_page
    if _daemon_page is None or _daemon_page.is_closed():
        if not _try_recover_page_wrapper():
            return None
    return _daemon_page


# ── 带锁的 daemon 操作函数（单实例模式，向后兼容）──

def daemon_send_message(message: str) -> dict:
    """在 daemon 持有的浏览器中发送消息。
    
    如果启用了 BrowserPool，从池子取实例；否则使用全局单实例。
    """
    global _daemon_page, _daemon_config, _daemon_pool
    
    # 池模式
    if _daemon_pool is not None:
        inst = _daemon_pool.acquire(timeout=60)
        if inst is None:
            return {"error": "浏览器池忙，请稍后再试"}
        try:
            # 打字策略集成
            from chat_adapters import find_and_fill_input
            input_el = find_and_fill_input(inst.page, _daemon_config)
            if input_el:
                _type_text_strategic(inst.page, input_el, message, _daemon_config)
            return send_message(inst.page, _daemon_config, message)
        finally:
            _daemon_pool.release(inst)
    
    # 单实例模式（向后兼容）
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        if _daemon_page is None or _daemon_page.is_closed():
            if not _try_recover_page_wrapper():
                return {"error": "浏览器未就绪，请稍后再试"}
        
        # 打字策略集成
        from chat_adapters import find_and_fill_input
        input_el = find_and_fill_input(_daemon_page, _daemon_config)
        if input_el:
            _type_text_strategic(_daemon_page, input_el, message, _daemon_config)
        
        return send_message(_daemon_page, _daemon_config, message)
    except Exception as e:
        sys.stderr.write(f"[Daemon] send_message 异常: {e}\n")
        return {"error": f"发送失败: {e}"}
    finally:
        _daemon_lock.release()


def daemon_do_review(content: str, context: str = "", deep_think: bool = True,
                     mode: str = "expert") -> dict:
    """在 daemon 持有的浏览器中执行评审。
    
    如果启用了 BrowserPool，从池子取实例；否则使用全局单实例。
    """
    global _daemon_page, _daemon_config, _daemon_session, _daemon_pool
    
    # 池模式
    if _daemon_pool is not None:
        inst = _daemon_pool.acquire(timeout=120)
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

            return send_message(inst.page, _daemon_config, full_message)
        finally:
            _daemon_pool.release(inst)
    
    # 单实例模式（向后兼容）
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        if _daemon_page is None or _daemon_page.is_closed():
            if not _try_recover_page_wrapper():
                return {"error": "浏览器未就绪，请稍后再试"}

        site_id = _daemon_session.get("site_id", "custom")
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
                switch_mode(_daemon_page, mode)
                if deep_think:
                    enable_deep_think(_daemon_page)
        except Exception:
            pass

        result = send_message(_daemon_page, _daemon_config, full_message)

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
        _daemon_lock.release()


def daemon_review_image(image_path: str, context: str = "", mode: str = "vision") -> dict:
    """在 daemon 浏览器中：切识图模式 → 上传图片 → 发送识图请求。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        global _daemon_page, _daemon_config, _daemon_session
        if _daemon_page is None or _daemon_page.is_closed():
            if not _try_recover_page_wrapper():
                return {"error": "浏览器未就绪，请稍后再试"}

        try:
            switch_mode(_daemon_page, mode)
        except Exception:
            pass

        full_prompt = "请仔细查看这张图片，描述你看到的所有内容。如果图片包含文本、数据、图表、错误信息等，请逐一解读。"
        if context:
            full_prompt = f"{context}\n\n{full_prompt}"

        sys.stderr.write(f"[识图] 上传图片: {image_path}\n")
        result = _send_with_image(_daemon_page, _daemon_config, image_path, full_prompt)

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
        _daemon_lock.release()


# ── Browser Agent 操作函数 ──

def daemon_screenshot(full_page: bool = False, quality: int = 80) -> dict:
    """截图并保存到本地，返回文件路径。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        page = _ensure_page()
        if page is None:
            return {"error": "浏览器未就绪"}
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"shot_{ts}.jpg"
        filepath = SCREENSHOT_DIR / filename
        page.screenshot(path=str(filepath), full_page=full_page, quality=quality, type="jpeg")
        sys.stderr.write(f"[Agent] 截图已保存: {filepath}\n")
        return {
            "status": "ok",
            "path": str(filepath),
            "filename": filename,
            "url": page.url,
            "title": page.title(),
        }
    except Exception as e:
        return {"error": f"截图失败: {e}"}
    finally:
        _daemon_lock.release()


def daemon_navigate(url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> dict:
    """导航到指定 URL。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        page = _ensure_page()
        if page is None:
            return {"error": "浏览器未就绪"}
        page.goto(url, wait_until=wait_until, timeout=timeout)
        sys.stderr.write(f"[Agent] 导航到: {url}\n")
        return {"status": "ok", "url": page.url, "title": page.title()}
    except Exception as e:
        return {"error": f"导航失败: {e}"}
    finally:
        _daemon_lock.release()


def daemon_click(selector: str = None, text: str = None, index: int = 0, timeout: int = 5000) -> dict:
    """点击元素：支持 CSS selector 或文本内容匹配。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        page = _ensure_page()
        if page is None:
            return {"error": "浏览器未就绪"}
        if selector:
            el = page.locator(selector).nth(index)
        elif text:
            el = page.get_by_text(text, exact=False).first
        else:
            return {"error": "需要 selector 或 text 参数"}
        if not el.is_visible(timeout=timeout):
            return {"error": f"元素不可见: {selector or text}"}
        tag = el.evaluate("el => el.tagName")
        el_text = el.inner_text()[:80] if el else ""
        el.click()
        sys.stderr.write(f"[Agent] 点击: <{tag}> {el_text}\n")
        time.sleep(0.5)
        return {"status": "ok", "clicked": f"<{tag}> {el_text}", "url": page.url, "title": page.title()}
    except Exception as e:
        return {"error": f"点击失败: {e}"}
    finally:
        _daemon_lock.release()


def daemon_scroll(direction: str = "down", amount: int = 300) -> dict:
    """滚动页面。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        page = _ensure_page()
        if page is None:
            return {"error": "浏览器未就绪"}
        if direction == "down":
            page.evaluate(f"window.scrollBy(0, {amount})")
        elif direction == "up":
            page.evaluate(f"window.scrollBy(0, -{amount})")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        else:
            return {"error": f"不支持的方向: {direction} (支持: up/down/top/bottom)"}
        scroll_y = page.evaluate("window.scrollY")
        return {"status": "ok", "direction": direction, "scrollY": scroll_y}
    except Exception as e:
        return {"error": f"滚动失败: {e}"}
    finally:
        _daemon_lock.release()


def daemon_read(selector: str = None) -> dict:
    """提取页面可见文本。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        page = _ensure_page()
        if page is None:
            return {"error": "浏览器未就绪"}
        if selector:
            el = page.locator(selector).first
            if not el.is_visible():
                return {"error": f"元素不可见: {selector}"}
            text = el.inner_text()
            tag = el.evaluate("el => el.tagName")
        else:
            text = page.locator("body").inner_text()
            tag = "body"
        max_chars = 10000
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + f"\n\n... (截断，原长度 {len(text)} 字符)"
        return {
            "status": "ok", "tag": tag, "text": text, "truncated": truncated,
            "total_chars": len(text) if not truncated else max_chars,
            "url": page.url, "title": page.title(),
            "scrollY": page.evaluate("window.scrollY"),
            "scrollHeight": page.evaluate("document.body.scrollHeight"),
        }
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    finally:
        _daemon_lock.release()


def daemon_type_text(selector: str, text: str, human_like: bool = True) -> dict:
    """在指定元素中输入文本。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        page = _ensure_page()
        if page is None:
            return {"error": "浏览器未就绪"}
        el = page.locator(selector).first
        if not el.is_visible():
            return {"error": f"元素不可见: {selector}"}
        el.click()
        el.fill("")
        from config import PASTE_THRESHOLD
        
        strategy = _daemon_type_strategy or TYPE_STRATEGY
        if strategy == "instant":
            _instant_input(page, el, text)
        elif strategy == "fast":
            _fast_type(page, el, text)
        elif human_like and len(text) < PASTE_THRESHOLD:
            _human_type(page, el, text)
        else:
            from browser_controller import human_paste
            human_paste(page, el, _daemon_config or {}, text)
        
        return {
            "status": "ok", "selector": selector, "length": len(text),
            "method": strategy,
        }
    except Exception as e:
        return {"error": f"输入失败: {e}"}
    finally:
        _daemon_lock.release()


def daemon_execute(js_code: str) -> dict:
    """在页面中执行 JavaScript。"""
    lock_err = _with_lock()
    if lock_err:
        return lock_err
    try:
        page = _ensure_page()
        if page is None:
            return {"error": "浏览器未就绪"}
        result = page.evaluate(js_code)
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"error": f"JS 执行失败: {e}"}
    finally:
        _daemon_lock.release()


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
        global _daemon_session, _daemon_config, _daemon_start_time
        if self.path == "/status":
            self._json_reply({
                "status": "ok",
                "site": _daemon_config.get("name", "unknown") if _daemon_config else "unknown",
                "url": _daemon_session.get("url", "") if _daemon_session else "",
            })
        elif self.path == "/health":
            self._json_reply({
                "status": "ok",
                "uptime": time.time() - _daemon_start_time,
                "browser_alive": not (_daemon_page is None or _daemon_page.is_closed()),
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
    """启动 HTTP daemon，保持浏览器长驻（向后兼容模式）。
    
    v4.1 新增：
    - pool_size: 浏览器池大小（>0 时启用池模式，默认 0=单实例）
    - type_strategy: 打字策略 (human/fast/instant)
    - route_mode: 路由模式 (auto/api/browser)
    
    Args:
        port: HTTP daemon 监听端口
        site: 站点标识
        url: 聊天页面 URL
        cdp: 是否通过 CDP 连接已有浏览器
        cdp_port: CDP 调试端口
        cdp_auto_launch: 是否自动启动浏览器
        cdp_browser_path: 自定义浏览器路径
        pool_size: 浏览器池大小（0=禁用池，使用单实例）
        type_strategy: 打字策略
        route_mode: 路由模式
    """
    from playwright.sync_api import sync_playwright
    from browser_controller import connect_via_cdp

    global _daemon_browser, _daemon_page, _daemon_config, _daemon_session, \
           _daemon_start_time, _daemon_playwright, _daemon_pool, _daemon_type_strategy

    _daemon_start_time = time.time()
    
    # 设置打字策略
    if type_strategy:
        _daemon_type_strategy = type_strategy
    
    # 设置路由模式
    if route_mode:
        from config import ROUTE_MODE as _route_cfg
        # 直接修改模块级变量（允许运行时切换）
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

    # P0-1: SESSION_FILE 不存在时自动 init
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

    # ── 浏览器池模式（pool_size > 0）──
    if cdp and pool_size > 0:
        from browser_pool import BrowserPool, set_pool
        sys.stderr.write(f"[Daemon] 启用浏览器池模式 (size={pool_size}, site={site_id})\n")
        _pool = BrowserPool(size=pool_size, site=site_id)
        _pool.start()
        set_pool(_pool)
        _daemon_pool = _pool
        # 从池子取一个实例作为 daemon 的默认页面（供 Browser Agent 操作）
        inst = _pool.acquire(timeout=60)
        if inst:
            _daemon_playwright = inst.playwright
            _daemon_browser = inst.browser
            _daemon_page = inst.page
            _init_ok = True
            print(f"[Daemon] 浏览器池已就绪 — {_daemon_config['name']} ({_daemon_session['url']})", flush=True)
            # 归还实例（池子模式：daemon_send_message 和 daemon_do_review 会重新 acquire）
            _pool.release(inst)
            # 启动心跳
            _daemon_heartbeat_stop.clear()
            hb = threading.Thread(target=_heartbeat_loop, daemon=True)
            hb.start()
        else:
            sys.stderr.write(f"[Daemon] 无法从池子获取实例\n")
            _init_ok = False
    elif cdp:
        # ── 单实例 CDP 模式（向后兼容）──
        from browser_controller import find_free_cdp_port, launch_edge_with_cdp, wait_for_cdp

        auto_launch = _daemon_session.get("cdp_auto_launch", True)
        browser_path = _daemon_session.get("cdp_browser_path", None)

        _init_ok = False
        for attempt in range(3):
            try:
                # 1) 找空闲端口（如果指定端口被占用）
                port_to_use = cdp_port
                if not wait_for_cdp(cdp_port, timeout=2):
                    free_port = find_free_cdp_port(start=cdp_port, max_attempts=10)
                    if free_port is None:
                        sys.stderr.write(f"[Daemon] CDP 端口 {cdp_port}-{cdp_port+9} 全部被占用，请先关闭占用进程\n")
                        break
                    port_to_use = free_port
                    if auto_launch:
                        sys.stderr.write(f"[Daemon] 端口 {cdp_port} 被占用，自动使用端口 {port_to_use}\n")
                        _daemon_session["cdp_port"] = port_to_use
                        cdp_port = port_to_use

                # 2) 启动浏览器（如果尚未就绪且允许自动启动）
                if not wait_for_cdp(port_to_use, timeout=2) and auto_launch:
                    sys.stderr.write(f"[Daemon] Edge 未启动，自动拉起 (端口 {port_to_use})...\n")
                    launch_edge_with_cdp(port_to_use, browser_path=browser_path)
                    if not wait_for_cdp(port_to_use, timeout=30):
                        sys.stderr.write(f"[Daemon] Edge 启动超时 (端口 {port_to_use})\n")
                        continue

                # 3) 连接
                _daemon_playwright, _daemon_browser, _daemon_page = connect_via_cdp(port_to_use)
                
                # P0: CDP 连接后显式导航到目标页面
                target_url = _daemon_session.get("url", SITE_CONFIGS.get(site_id, {}).get("url", ""))
                max_nav_retries = 3
                for nav_attempt in range(max_nav_retries):
                    try:
                        _daemon_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(2)
                        if target_url in _daemon_page.url:
                            sys.stderr.write(f"[Daemon] CDP 页面验证通过: {_daemon_page.url}\n")
                            break
                        sys.stderr.write(f"[Daemon] 页面 URL 异常 ({_daemon_page.url}), 重试 {nav_attempt+1}/{max_nav_retries}\n")
                    except Exception as nav_e:
                        sys.stderr.write(f"[Daemon] 导航异常: {nav_e}, 重试 {nav_attempt+1}/{max_nav_retries}\n")
                        time.sleep(2)
                else:
                    sys.stderr.write(f"[Daemon] CDP 导航失败，当前页面: {_daemon_page.url}，后续操作可能受影响\n")
                
                if site_id == "deepseek":
                    try:
                        switch_mode(_daemon_page, "expert")
                        enable_deep_think(_daemon_page)
                    except Exception:
                        pass
                _init_ok = True
                print(f"[Daemon] CDP 浏览器已连接 — {_daemon_config['name']} ({_daemon_session['url']})", flush=True)
                # 启动心跳
                _daemon_heartbeat_stop.clear()
                hb = threading.Thread(target=_heartbeat_loop, daemon=True)
                hb.start()
                break
            except Exception as e:
                sys.stderr.write(f"[Daemon] CDP 浏览器连接失败(尝试{attempt+1}/3): {e}\n")
                time.sleep(3)
    else:
        # ── 非 CDP 模式（persistent_context）──
        _init_ok = False
        for attempt in range(5):
            try:
                _daemon_playwright = sync_playwright().start()
                _daemon_browser, _daemon_page = get_existing_page(_daemon_playwright, _daemon_session)
                if site_id == "deepseek":
                    try:
                        switch_mode(_daemon_page, "expert")
                        enable_deep_think(_daemon_page)
                    except Exception:
                        pass
                print(f"[Daemon] 浏览器已就绪 — {_daemon_config['name']} ({_daemon_session['url']})", flush=True)
                _init_ok = True
                break
            except Exception as e:
                sys.stderr.write(f"[Daemon] 浏览器初始化失败(尝试{attempt+1}/5): {e}\n")
                try:
                    if _daemon_playwright:
                        _daemon_playwright.stop()
                        _daemon_playwright = None
                except Exception:
                    pass
                time.sleep(5)

    if not _init_ok:
        sys.stderr.write(f"[Daemon] 浏览器初始化失败(已重试5次)，HTTP 服务仍可用但浏览器操作不可用\n")

    from browser_controller import get_existing_page

    # 启动 HTTP 服务
    server = http.server.HTTPServer(("127.0.0.1", port), DaemonHandler)
    print(f"[Daemon] HTTP 服务: http://127.0.0.1:{port}", flush=True)
    print(f"[Daemon] Actor-Critic: POST /send | /review | /review-image | /cache/clear", flush=True)
    print(f"[Daemon] Browser Agent: POST /navigate | /screenshot | /read | /click | /scroll | /type | /execute", flush=True)
    print(f"[Daemon] 管理: GET /status | /health | POST /shutdown", flush=True)
    if _daemon_pool:
        print(f"[Daemon] 浏览器池: {len(_daemon_pool.instances)} 实例 | 策略: {_daemon_type_strategy}", flush=True)
    else:
        print(f"[Daemon] 打字策略: {_daemon_type_strategy}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Daemon] 正在关闭...", flush=True)
    finally:
        server.server_close()
        try:
            if _daemon_pool:
                # 关闭池子
                for inst in _daemon_pool.instances:
                    try:
                        if inst.playwright:
                            inst.playwright.stop()
                    except Exception:
                        pass
            else:
                _daemon_browser.close()
        except Exception:
            pass
        try:
            if _daemon_playwright:
                _daemon_playwright.stop()
        except Exception:
            pass