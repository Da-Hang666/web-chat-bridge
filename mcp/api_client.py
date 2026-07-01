"""
web-chat-bridge MCP — API 直连客户端 (v5.1)

绕过浏览器，直接调用 DeepSeek Chat Completions API。
适用场景：
  - 纯文本查询（无图片、无网页搜索需求）
  - send_message 的快速路径
  - review_text 的 Critic 评审（不走浏览器打字+等待）

使用方式：
  result = api_send_message("你好")
  result = api_do_review("要评审的内容", context="背景说明")
"""

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


# ── API 配置（从 ~/.deepseek/config.toml 读取）──

def _read_api_config() -> dict:
    """读取 DeepSeek API 配置。"""
    config_path = Path.home() / ".deepseek" / "config.toml"
    cfg = {"api_key": "", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash"}
    
    if not config_path.exists():
        sys.stderr.write("[API] config.toml 未找到，API 直连不可用\n")
        return cfg
    
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key == "api_key" and val:
                cfg["api_key"] = val
            elif key == "base_url" and val:
                cfg["base_url"] = val
            elif key == "model" and val:
                cfg["model"] = val
    except Exception as e:
        sys.stderr.write(f"[API] 配置读取失败: {e}\n")
    
    return cfg


_API_CONFIG = None


def get_api_config() -> dict:
    """获取 API 配置（懒加载）。"""
    global _API_CONFIG
    if _API_CONFIG is None:
        _API_CONFIG = _read_api_config()
    return _API_CONFIG


def _call_api(messages: list, model: str = None, temperature: float = 0.0,
              max_tokens: int = 4096, timeout: int = 60) -> dict:
    """调用 DeepSeek Chat Completions API。"""
    cfg = get_api_config()
    if not cfg["api_key"]:
        return {"error": "API Key 未配置，无法使用 API 直连"}
    
    if model is None:
        model = cfg["model"]
    
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    
    try:
        start = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - start
        
        choice = body.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "")
        
        return {
            "response": content,
            "model": body.get("model", model),
            "elapsed": round(elapsed, 2),
            "usage": body.get("usage", {}),
        }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"error": f"API HTTP {e.code}: {error_body[:300]}"}
    except Exception as e:
        return {"error": f"API 调用失败: {e}"}


# ── 公开接口 ──

def api_send_message(message: str, model: str = None) -> dict:
    """通过 API 发送消息，返回 AI 回复。"""
    sys.stderr.write(f"[API] 发送消息 ({len(message)} 字符)...\n")
    result = _call_api(
        messages=[{"role": "user", "content": message}],
        model=model,
    )
    if "error" not in result:
        result["sent"] = message
        result["timestamp"] = time.time()
        result["_route"] = "api"
        elapsed = result.get("elapsed", 0)
        sys.stderr.write(f"[API] 完成 ({elapsed}s)\n")
    return result


_REVIEW_SYSTEM = """你是一个严格的技术评审员（Critic）。请审查以下内容。

评审规则：
1. 检查事实错误、逻辑漏洞、代码 bug
2. 检查遗漏的边界情况
3. 评估是否满足隐含需求
4. 给出 0-10 的评分

请用 JSON 格式回复（不要多余文字，只要 JSON）：
{"verdict": "pass|warn|fail", "score": <0-10>, "issues": ["问题1", ...], "suggestions": ["改进1", ...], "summary": "一句话总结"}
"""


def api_do_review(content: str, context: str = "", model: str = None) -> dict:
    """通过 API 直连评审内容（绕过浏览器）。"""
    user_msg = content
    if context:
        user_msg = f"上下文：{context}\n\n{content}"
    
    sys.stderr.write(f"[API] 评审 ({len(user_msg)} 字符)...\n")
    result = _call_api(
        messages=[
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        model=model or "deepseek-v4-pro",  # 评审用 Pro 模型
        timeout=120,
    )
    
    if "error" in result:
        return result
    
    # 尝试解析 JSON 回复
    from review_engine import parse_review_json
    review = parse_review_json(result["response"])
    review["raw"] = result["response"]
    review["_route"] = "api"
    review["_meta"] = {
        "model": result.get("model"),
        "elapsed": result.get("elapsed"),
        "content_chars": len(content),
    }
    
    elapsed = result.get("elapsed", 0)
    sys.stderr.write(f"[API] 评审完成 ({elapsed}s)\n")
    return review


def api_available() -> bool:
    """检查 API 直连是否可用。"""
    cfg = get_api_config()
    return bool(cfg["api_key"])
