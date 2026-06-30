"""
web-chat-bridge MCP — 各平台适配层

从 v3 提取的站点适配逻辑：
- get_site_config — URL → 站点配置
- find_and_fill_input — 每个平台不同的 DOM 选择器
- enable_deep_think — DeepSeek Chat 深度思考
- switch_mode — 模式切换 (fast/expert/vision)
"""

import random
import sys
import time
from config import SITE_CONFIGS


def get_site_config(url: str):
    """根据 URL 返回 (site_id, config_dict)。"""
    for key, cfg in SITE_CONFIGS.items():
        if key in url.lower():
            return key, cfg
    return "custom", SITE_CONFIGS["custom"]


def find_and_fill_input(page, config):
    """在页面中找到可见的输入框并聚焦清空。返回输入框元素或 None。"""
    selectors = config["input_selector"].split(", ")
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1000):
                el.click()
                el.fill("")
                return el
        except Exception:
            continue
    try:
        return page.locator('textarea:visible').first
    except Exception:
        pass
    try:
        return page.locator('[contenteditable="true"]:visible').first
    except Exception:
        pass
    return None


def enable_deep_think(page):
    """在 DeepSeek Chat 页面开启深度思考模式。"""
    try:
        toggles = page.locator('.ds-toggle-button').all()
        for toggle in toggles:
            try:
                text = toggle.inner_text().strip()
                if '深度思考' in text or 'Deep Think' in text or 'DeepThink' in text:
                    cls = toggle.get_attribute('class') or ''
                    if '--selected' not in cls:
                        toggle.click()
                        time.sleep(1)
                        sys.stderr.write("[Critic] 深度思考已开启\n")
                    else:
                        sys.stderr.write("[Critic] 深度思考已处于开启状态\n")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def switch_mode(page, mode: str = "expert"):
    """切换 DeepSeek Chat 模式：fast / expert / vision。
    
    基于实测 DOM：三个 <DIV role="radio"> 横向排列在 role="radiogroup" 容器中。
    """
    mode_map = {
        "expert": "专家模式",
        "fast": "快速模式",
        "vision": "识图模式",
    }
    target_text = mode_map.get(mode, mode)
    
    try:
        radios = page.locator('[role="radio"]').all()
        for radio in radios:
            try:
                text = radio.inner_text().strip()
                if target_text in text:
                    radio.click()
                    time.sleep(1)
                    sys.stderr.write(f"[Critic] 已切换到: {target_text}\n")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False