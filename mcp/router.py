"""
分层路由器 — API 优先，浏览器作为能力增强 fallback。

路由决策规则：
1. 纯文本对话（无特殊需求） → API（deepseek-v4-flash / pro）
2. 需要深度思考链展示 → 浏览器
3. 需要专家模式 / 自定义指令 → 浏览器
4. 需要联网搜索 + 深度推理 → 浏览器
5. 需要多模态识图（图片理解） → 浏览器
6. 触发 Actor-Critic 评审 → 浏览器
7. 任意网页聊天 UI 交互 → 浏览器
"""

from config import ROUTE_MODE, API_MODEL


def should_use_browser(
    message: str = "",
    mode: str = "expert",
    require_images: bool = False,
    require_web_search: bool = False,
    require_deep_think: bool = False,
    require_actor_critic: bool = False,
) -> bool:
    """根据需求判断是否走浏览器路径。"""
    
    # 强制路由覆盖
    if ROUTE_MODE == "api":
        return False
    if ROUTE_MODE == "browser":
        return True
    
    # 自动路由决策
    if require_deep_think or mode in ("expert", "vision"):
        return True
    if require_images or require_web_search or require_actor_critic:
        return True
    
    return False