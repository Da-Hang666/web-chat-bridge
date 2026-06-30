"""
web-chat-bridge MCP — 评审引擎

从 v3 提取的 Actor-Critic 评审逻辑：
- do_review — 将产出发给 AI 模型评审，返回结构化 JSON
- parse_review_json — 从模型回复中提取 JSON 评审结果
- loop_mode — 持续对话模式
- single_send — 单次发送（非 daemon 模式）
"""

import json
import re
import sys
import time
from pathlib import Path

from browser_controller import get_existing_page, send_message, enable_deep_think, switch_mode
from config import SESSION_FILE, SITE_CONFIGS, MAX_CONTENT_CHARS
from chat_adapters import get_site_config


# ── Actor-Critic 评审提示词模板 ──
CRITIC_PROMPT = """你是一个严格的技术评审员（Critic）。下面是一个 AI Agent 产出的工作结果，请审查。

评审规则：
1. 检查事实错误、逻辑漏洞、代码 bug
2. 检查遗漏的边界情况
3. 评估是否满足隐含需求
4. 给出 0-10 的评分

请用 JSON 格式回复（不要多余文字，只要 JSON）：
{"verdict": "pass|warn|fail", "score": <0-10>, "issues": ["问题1", ...], "suggestions": ["改进1", ...], "summary": "一句话总结"}

---

产出内容：
"""


def parse_review_json(raw_response: str) -> dict:
    """尝试从 Critic 回复中提取 JSON 评审结果。"""
    # 直接尝试解析整个回复
    try:
        return json.loads(raw_response)
    except json.JSONDecodeError:
        pass

    # 尝试提取 JSON 块（markdown 代码块）
    for pattern in [r'```json\s*([\s\S]*?)\s*```', r'```\s*([\s\S]*?)\s*```']:
        m = re.search(pattern, raw_response)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue

    # 从 "verdict" 定位，匹配最邻近的平衡括号 JSON
    verdict_idx = raw_response.find('"verdict"')
    if verdict_idx != -1:
        start = raw_response.rfind('{', 0, verdict_idx)
        if start != -1:
            depth = 0
            end = -1
            for i in range(start, len(raw_response)):
                if raw_response[i] == '{':
                    depth += 1
                elif raw_response[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end != -1:
                try:
                    return json.loads(raw_response[start:end])
                except json.JSONDecodeError:
                    candidate = raw_response[start:end]
                    if candidate.rstrip().endswith(','):
                        candidate = candidate.rstrip()[:-1]
                    try:
                        return json.loads(candidate + '}')
                    except json.JSONDecodeError:
                        pass

    return {
        "verdict": "unknown",
        "score": None,
        "issues": [],
        "suggestions": [],
        "summary": raw_response[:500],
    }


def do_review(content: str, context: str = "", deep_think: bool = True,
              mode: str = "expert", site: str = None, url: str = None) -> dict:
    """Actor-Critic 评审：将产出发给 Critic，返回结构化评审。"""
    from playwright.sync_api import sync_playwright

    if not SESSION_FILE.exists():
        return {"error": "请先运行 --init 初始化"}

    session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    if site and site in SITE_CONFIGS and site != "custom":
        site_id = site
        session["site_id"] = site
        session["url"] = SITE_CONFIGS[site].get("url", url or session["url"])
    elif url:
        site_id, _ = get_site_config(url)
        session["url"] = url
        session["site_id"] = site_id
    else:
        site_id = session.get("site_id", "custom")
    config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])

    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + f"\n\n... (截断，原长度 {len(content)} 字符)"

    prompt = CRITIC_PROMPT
    if context:
        prompt = prompt + f"\n\n上下文：{context}\n"
    if len(content) > 4000:
        prompt = prompt.replace("产出内容：", "产出内容（长文本，请重点关注关键部分）：")

    full_message = prompt + content
    sys.stderr.write(f"[Critic] 发送 {len(full_message)} 字符，等待评审...\n")

    with sync_playwright() as p:
        browser, page = get_existing_page(p, session)
        try:
            if site_id == "deepseek":
                switch_mode(page, mode)
                if deep_think:
                    enable_deep_think(page)
            result = send_message(page, config, full_message)
        finally:
            browser.close()

    if "error" in result:
        return result

    review = parse_review_json(result["response"])
    review["raw"] = result["response"]
    review["_meta"] = {
        "site": config["name"],
        "content_chars": len(content),
        "sent_chars": len(full_message),
        "timestamp": result.get("timestamp"),
    }
    return review


def loop_mode():
    """持续对话模式（CLI 交互式）。"""
    from playwright.sync_api import sync_playwright

    if not SESSION_FILE.exists():
        print("[ERR] 请先运行 --init")
        return

    session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    site_id = session.get("site_id", "custom")
    config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])

    print(f"[LINK] {config['name']} ({session['url']})")
    print("   输入 /quit 退出, /review 评审模式")
    print("   " + "-" * 50)

    with sync_playwright() as p:
        browser, page = get_existing_page(p, session)
        try:
            while True:
                try:
                    user_input = input("\nDeepSeek > ")
                except (EOFError, KeyboardInterrupt):
                    break

                if not user_input.strip():
                    continue
                if user_input.strip().lower() in ("/quit", "/exit", "/q"):
                    break

                result = send_message(page, config, user_input)
                if "error" in result:
                    print(f"[ERR] {result['error']}")
                    continue
                print(f"\n[WEB] ->\n{result['response']}")
                print("-" * 50)
        finally:
            browser.close()


def single_send(message: str):
    """单次发送消息（非 daemon 模式）。"""
    from playwright.sync_api import sync_playwright

    if not SESSION_FILE.exists():
        print(json.dumps({"error": "请先运行 --init"}, ensure_ascii=False))
        return

    session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    site_id = session.get("site_id", "custom")
    config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])

    with sync_playwright() as p:
        browser, page = get_existing_page(p, session)
        try:
            result = send_message(page, config, message)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            browser.close()


# ═══════════════════════════════════════════════════════════════
# 异步评审（v4.1）
# ═══════════════════════════════════════════════════════════════

import threading as _threading

def do_review_async(content: str, context: str = "", mode: str = "expert",
                    callback=None) -> _threading.Thread:
    """异步评审：在后台线程执行，通过 callback 返回结果。

    Args:
        content: 要评审的文本/代码
        context: 评审上下文
        mode: 模式 (fast/expert/vision)
        callback: 可选回调函数，接收评审结果 dict

    Returns:
        threading.Thread 对象（daemon=True）
    """
    def _run():
        result = do_review(content, context=context, mode=mode)
        if callback:
            try:
                callback(result)
            except Exception:
                pass
    
    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    return t
