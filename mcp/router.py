"""
分层路由器 v5.1 — API 优先，浏览器作为能力增强 fallback。

路由决策规则：
1. API 可用 → 优先走 API（DeepSeek Chat Completions）
2. 需要识图 / 联网搜索 / 网页交互 → 浏览器
3. API 不可用或失败 → 降级到浏览器
"""

from config import ROUTE_MODE


def can_use_api(
    require_images: bool = False,
    require_web_search: bool = False,
    require_web_interaction: bool = False,
) -> bool:
    """判断请求能否走 API 直连（无需浏览器）。"""
    # 强制路由覆盖
    if ROUTE_MODE == "browser":
        return False
    if ROUTE_MODE == "api":
        return True
    
    # 需要网页交互的必须走浏览器
    if require_images or require_web_search or require_web_interaction:
        return False
    
    return True


def should_use_browser(
    message: str = "",
    mode: str = "expert",
    require_images: bool = False,
    require_web_search: bool = False,
    require_deep_think: bool = False,
    require_actor_critic: bool = False,
    require_web_interaction: bool = False,
) -> bool:
    """根据需求判断是否走浏览器路径。
    
    v5.1: 默认优先 API，仅必要时走浏览器。
    """
    
    # 强制路由覆盖
    if ROUTE_MODE == "api":
        return False
    if ROUTE_MODE == "browser":
        return True
    
    # 自动路由决策 — v5.1: 只在这些条件下走浏览器
    if require_images or require_web_search:
        return True
    if require_web_interaction:
        return True
    # v5.1: 深度思考 / 专家模式不再强制走浏览器（API 也支持 reasoning）
    if require_deep_think and not _api_supports_deep_think():
        return True
    # v5.1: Actor-Critic 评审优先走 API（api_do_review 已支持）
    if require_actor_critic:
        return False
    
    return False


def _api_supports_deep_think() -> bool:
    """检查 API 是否支持深度思考 / reasoning。"""
    # v5.1: DeepSeek API 支持 reasoning_effort 参数
    from api_client import api_available
    return api_available()
