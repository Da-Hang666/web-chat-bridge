"""
浏览器地图引擎 — 三层实时空间-语义模型。

几何层（bbox） + 语义层（AXTree） + 查询引擎。
Agent 用自然语言描述目标，引擎返回精确坐标/元素句柄。
"""

import json
import math
import sys
import time
from pathlib import Path
from typing import Optional


class MapNode:
    """地图上的一个可交互节点。"""
    def __init__(self, node_id: str, role: str, name: str, 
                 x: float, y: float, w: float, h: float,
                 description: str = "", value: str = "",
                 parent_id: str = None, children_ids: list = None):
        self.node_id = node_id
        self.role = role          # button / textbox / link / heading / switch / ...
        self.name = name          # aria-label / title / innerText
        self.description = description
        self.value = value
        # 几何（视口坐标）
        self.x = x
        self.y = y
        self.width = w
        self.height = h
        # 中心点
        self.cx = x + w / 2
        self.cy = y + h / 2
        # 树关系
        self.parent_id = parent_id
        self.children_ids = children_ids or []
    
    @property
    def center(self) -> tuple:
        return (self.cx, self.cy)
    
    @property
    def area(self) -> float:
        return self.width * self.height
    
    def distance_to(self, other: "MapNode") -> float:
        return math.hypot(self.cx - other.cx, self.cy - other.cy)
    
    def to_dict(self) -> dict:
        return {
            "id": self.node_id, "role": self.role, "name": self.name,
            "description": self.description, "value": self.value,
            "x": self.x, "y": self.y, "w": self.width, "h": self.height,
            "cx": self.cx, "cy": self.cy,
            "parent": self.parent_id, "children": self.children_ids,
        }


class PageMap:
    """单页地图：几何 + 语义双层索引。"""
    
    def __init__(self, page_url: str = ""):
        self.page_url = page_url
        self.nodes: dict[str, MapNode] = {}       # node_id → MapNode
        self.by_role: dict[str, list[str]] = {}    # role → [node_ids]
        self.by_name: dict[str, list[str]] = {}    # name_token → [node_ids]
        self._built_at = 0.0
    
    def add_node(self, node: MapNode):
        self.nodes[node.node_id] = node
        # role 索引
        role = node.role.lower()
        self.by_role.setdefault(role, []).append(node.node_id)
        # name 分词索引
        for token in _tokenize(node.name):
            self.by_name.setdefault(token, []).append(node.node_id)
    
    def spatial_query(self, cx: float, cy: float, radius: float) -> list[MapNode]:
        """空间范围查询：返回中心点 (cx,cy) 半径 radius 内的所有节点。"""
        results = []
        for node in self.nodes.values():
            if math.hypot(node.cx - cx, node.cy - cy) <= radius:
                results.append(node)
        results.sort(key=lambda n: math.hypot(n.cx - cx, n.cy - cy))
        return results
    
    def semantic_query(self, role: str = None, name_contains: str = None) -> list[MapNode]:
        """语义查询：按 role 和 name 筛选。"""
        candidates = set(self.nodes.keys())
        if role:
            role_lower = role.lower()
            candidates &= set(self.by_role.get(role_lower, []))
        if name_contains:
            tokens = _tokenize(name_contains)
            name_set = set()
            for t in tokens:
                name_set.update(self.by_name.get(t, []))
            candidates &= name_set
        return [self.nodes[nid] for nid in candidates]
    
    def query(self, role: str = None, name_contains: str = None,
              near_node_id: str = None, near_radius: float = 200,
              below_node_id: str = None, below_range: float = 300,
              above_node_id: str = None, above_range: float = 300) -> list[MapNode]:
        """
        联合查询：语义 + 空间约束。
        
        空间约束可选：
        - near_node_id: 在该节点附近搜索
        - below_node_id: 在该节点下方搜索（y 更大）
        - above_node_id: 在该节点上方搜索（y 更小）
        """
        candidates = self.semantic_query(role=role, name_contains=name_contains)
        
        # 空间过滤
        if near_node_id and near_node_id in self.nodes:
            anchor = self.nodes[near_node_id]
            candidates = [n for n in candidates 
                         if n.distance_to(anchor) <= near_radius]
        if below_node_id and below_node_id in self.nodes:
            anchor = self.nodes[below_node_id]
            candidates = [n for n in candidates 
                         if n.cy > anchor.cy and n.cy - anchor.cy <= below_range]
        if above_node_id and above_node_id in self.nodes:
            anchor = self.nodes[above_node_id]
            candidates = [n for n in candidates 
                         if n.cy < anchor.cy and anchor.cy - n.cy <= above_range]
        
        # 按面积排序（大的通常是容器，小的才是目标按钮）
        candidates.sort(key=lambda n: (n.area, math.hypot(n.cx - 500, n.cy - 500)))
        return candidates


def _tokenize(text: str) -> list[str]:
    """中文按字符，英文按空格/标点分词。"""
    if not text:
        return []
    tokens = []
    buf = ""
    for ch in text:
        if ch.isascii() and ch.isalpha():
            buf += ch
        else:
            if buf:
                tokens.append(buf.lower())
                buf = ""
            if ch.strip():
                tokens.append(ch)
    if buf:
        tokens.append(buf.lower())
    return tokens


# ═══════════════════════════════════════════════════════
# CDP 采集函数
# ═══════════════════════════════════════════════════════

def collect_geometric_nodes(page) -> list[dict]:
    """通过 JS 采集视口内所有可交互元素的几何信息。"""
    if page is None:
        raise TypeError("page must not be None")
    return page.evaluate("""() => {
        const results = [];
        const interactive = ['button','input','textarea','select','a','[role]',
                             '[contenteditable]','[tabindex]','[onclick]',
                             'label','summary','details'];
        const selector = interactive.join(',');
        const elements = document.querySelectorAll(selector);
        let id = 0;
        
        elements.forEach(el => {
            const rect = el.getBoundingClientRect();
            // 跳过不可见元素
            if (rect.width === 0 && rect.height === 0) return;
            if (rect.bottom < 0 || rect.top > window.innerHeight) return;
            if (rect.right < 0 || rect.left > window.innerWidth) return;
            
            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') return;
            
            const role = el.getAttribute('role') || el.tagName.toLowerCase();
            const name = el.getAttribute('aria-label') 
                      || el.getAttribute('title') 
                      || el.getAttribute('placeholder')
                      || (el.textContent || '').trim().slice(0, 100)
                      || '';
            
            results.push({
                id: 'geo-' + (id++),
                role: role,
                name: name,
                description: el.getAttribute('aria-description') || '',
                value: el.value || el.getAttribute('aria-valuetext') || '',
                x: rect.x, y: rect.y,
                w: rect.width, h: rect.height,
                parent: null,
                children: [],
            });
        });
        return results;
    }""")


def quick_find(page_map: PageMap, target_description: str) -> list[MapNode]:
    """
    高频快捷查询。将自然语言描述解析为 role + name_contains 联合查询。
    
    支持的描述格式（逗号分隔）：
    - "button, 发送"     → role=button, name_contains=发送
    - "textbox, 消息"    → role=textbox, name_contains=消息  
    - "switch, 深度思考" → role=switch, name_contains=深度思考
    - "button, 登录"     → role=button, name_contains=登录
    """
    if not target_description or not target_description.strip():
        return []
    parts = [p.strip() for p in target_description.split(",")]
    role = parts[0] if len(parts) >= 1 else None
    name = parts[1] if len(parts) >= 2 else None
    return page_map.query(role=role, name_contains=name)