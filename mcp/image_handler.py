"""
web-chat-bridge MCP — 图片处理

从 v3 提取的图片上传与识图逻辑：
- _upload_image — 通过 file input 或 page.route 虚拟文件服务上传
- _wait_for_image_preview — 等待图片预览加载
- _send_with_image — 上传图片 + 附带文本 + 发送 + 等待回复
"""

import sys
import time
from pathlib import Path

from browser_controller import human_type, human_paste, _wait_for_response
from chat_adapters import find_and_fill_input
from config import PASTE_THRESHOLD


def _upload_image(page, image_path: str):
    """上传图片到聊天页面。file input 优先，找不到则模拟拖拽丢图。"""
    if not Path(image_path).exists():
        return False, f"图片文件不存在: {image_path}"

    # ── 策略 1：file input ──
    file_inputs = page.locator('input[type="file"]').all()
    
    if not file_inputs:
        attach_selectors = [
            'button[aria-label*="附件" i]', 'button[aria-label*="上传" i]',
            '[class*="upload-btn"]', '[class*="attach"]', 'button[class*="upload"]',
        ]
        for sel in attach_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    time.sleep(1.0)
                    file_inputs = page.locator('input[type="file"]').all()
                    if file_inputs:
                        break
            except Exception:
                continue

    if file_inputs:
        try:
            file_inputs[0].set_input_files(image_path)
            sys.stderr.write(f"[图片] file input 上传: {Path(image_path).name}\n")
            _wait_for_image_preview(page)
            return True, None
        except Exception as e:
            return False, f"上传失败: {e}"

    # ── 策略 2：page.route 虚拟文件服务 + 拖拽/粘贴 ──
    sys.stderr.write(f"[图片] route 上传: {Path(image_path).name}\n")
    try:
        data = Path(image_path).read_bytes()
        ext = Path(image_path).suffix.lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }
        mime = mime_map.get(ext, "image/png")
        filename = Path(image_path).name
        
        VIRTUAL_URL = "__local_img__"
        page.route(f"**/{VIRTUAL_URL}**", lambda route: route.fulfill(
            body=data, content_type=mime
        ))
        
        page.evaluate("""
            async (virtualUrl, mime, filename) => {
                const resp = await fetch(virtualUrl);
                const blob = await resp.blob();
                const file = new File([blob], filename, {type: mime});
                
                const dt = new DataTransfer();
                dt.items.add(file);
                
                const target = document.querySelector('textarea') 
                    || document.querySelector('[contenteditable="true"]')
                    || document.querySelector('[class*="input"]');
                if (!target) return;
                target.focus();
                
                target.dispatchEvent(new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt
                }));
                
                ['dragenter', 'dragover'].forEach(evtName => {
                    target.dispatchEvent(new DragEvent(evtName, {
                        bubbles: true, cancelable: true, dataTransfer: dt
                    }));
                });
                target.dispatchEvent(new DragEvent('drop', {
                    bubbles: true, cancelable: true, dataTransfer: dt
                }));
            }
        """, [VIRTUAL_URL, mime, filename])
        
        try:
            page.unroute(f"**/{VIRTUAL_URL}**")
        except Exception:
            pass
        
        sys.stderr.write(f"[图片] route 完成: {filename}\n")
        _wait_for_image_preview(page)
        return True, None
    except Exception as e:
        return False, f"拖拽上传失败: {e}"


def _wait_for_image_preview(page):
    """等待图片预览/缩略图出现，最多 20 秒。"""
    loaded = False
    for _ in range(20):
        time.sleep(1)
        preview_selectors = [
            'img[src*="blob"]', '[class*="preview"] img', '[class*="thumbnail"] img',
            '[class*="upload"] img', '.ds-upload-preview img',
        ]
        for psel in preview_selectors:
            try:
                if page.locator(psel).count() > 0:
                    loaded = True
                    break
            except Exception:
                continue
        if loaded:
            break
    if loaded:
        time.sleep(1.0)
        sys.stderr.write("[图片] 预览加载完成\n")
    else:
        sys.stderr.write("[图片] 未检测到预览，继续\n")


def _send_with_image(page, config, image_path: str, text_prompt: str = ""):
    """上传图片并附带文本，然后发送。"""
    # 1. 上传图片
    ok, err = _upload_image(page, image_path)
    if not ok:
        return {"error": err}

    # 2. 如果有文本，输入文本
    if text_prompt:
        input_el = find_and_fill_input(page, config)
        if input_el is None:
            return {"error": "找不到输入框"}
        if len(text_prompt) >= PASTE_THRESHOLD:
            human_paste(page, input_el, config, text_prompt)
        else:
            human_type(page, input_el, text_prompt)

    # 3. 发送
    send_selectors = config["send_button"].split(", ")
    sent = False
    for sel in send_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000) and btn.is_enabled():
                btn.click()
                sent = True
                break
        except Exception:
            continue
    if not sent:
        try:
            page.keyboard.press("Enter")
            sent = True
        except Exception:
            return {"error": "无法点击发送按钮"}

    sys.stderr.write("[图片] 已发送，等待回复...\n")

    # 4. 等待回复（重试验证：排除仅含 UI 占位文本的无效回复）
    for attempt in range(2):
        result = _wait_for_response(page, config, text_prompt or "(图片)")
        resp = result.get("response", "")
        skip_phrases = ["使用识图模式开始对话", "深度思考", "AI 生成可能有误"]
        is_placeholder = len(resp) < 80 and any(p in resp for p in skip_phrases)
        if is_placeholder and attempt == 0:
            sys.stderr.write("[图片] 回复为占位文本，等待 3 秒后重试发送...\n")
            time.sleep(3)
            page.keyboard.press("Enter")
            continue
        return result

    return result