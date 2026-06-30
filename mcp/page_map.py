"""语义页面地图 PageMap v2 — 人眼级识别 + 几何节点采集 + 语义索引 + 可见性检测

设计目标：
1. 生成页面可交互元素的几何/语义清单
2. 支持按 role/name/坐标 快速查询
3. 排除不可见/装饰性/被遮挡/禁用元素
4. 置信度评分排序 + 去重
5. 跨站点通用，不依赖特定 CSS class
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional
from math import hypot

logger = logging.getLogger(__name__)


@dataclass
class MapNode:
    """页面地图中的单个可交互节点"""
    id: str                        # 稳定 ID（geo-uuid）
    role: str                      # 语义角色
    name: str                      # 可见/语义名称
    x: float
    y: float
    w: float
    h: float
    styles: dict[str, object] = field(default_factory=dict)
    # v2 新增字段
    tag: str = ""
    disabled: bool = False
    occluded: bool = True
    cursor: str = ""

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def width(self) -> float:
        return self.w

    @property
    def height(self) -> float:
        return self.h

    @property
    def area(self) -> float:
        return self.w * self.h

    def distance_to(self, other: MapNode) -> float:
        return hypot(self.cx - other.cx, self.cy - other.cy)

    def to_dict(self, score: float = None) -> dict:
        d = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "tag": self.tag,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "w": round(self.w, 1),
            "h": round(self.h, 1),
            "area": round(self.area, 1),
        }
        if score is not None:
            d["score"] = round(score, 3)
        return d


_ROLE_REWRITE: dict[str, str] = {
    "button": "button",
    "link": "link",
    "textbox": "textbox",
    "searchbox": "searchbox",
    "heading": "heading",
    "checkbox": "checkbox",
    "radio": "radio",
    "switch": "switch",
    "tab": "tab",
    "menuitem": "menuitem",
    "combobox": "combobox",
    "listbox": "listbox",
    "option": "option",
    "slider": "slider",
    "progressbar": "progressbar",
    "img": "img",
    "navigation": "navigation",
    "banner": "banner",
    "contentinfo": "contentinfo",
    "form": "form",
    "main": "main",
    "complementary": "complementary",
    "region": "region",
    "separator": "separator",
    "tooltip": "tooltip",
    "dialog": "dialog",
    "alert": "alert",
    "log": "log",
    "status": "status",
    "timer": "timer",
    "marquee": "marquee",
    "application": "application",
    "group": "group",
    "tree": "tree",
    "treeitem": "treeitem",
    "grid": "grid",
    "gridcell": "gridcell",
    "row": "row",
    "rowgroup": "rowgroup",
    "columnheader": "columnheader",
    "rowheader": "rowheader",
    "table": "table",
    "cell": "cell",
    "list": "list",
    "listitem": "listitem",
    "menu": "menu",
    "menubar": "menubar",
    "spinbutton": "spinbutton",
    "scrollbar": "scrollbar",
    "article": "article",
    "document": "document",
    "feed": "feed",
    "figure": "figure",
    "graphics-document": "graphics-document",
    "graphics-object": "graphics-object",
    "graphics-symbol": "graphics-symbol",
    "meter": "meter",
    "blockquote": "blockquote",
    "caption": "caption",
    "definition": "definition",
    "deletion": "deletion",
    "emphasis": "emphasis",
    "insertion": "insertion",
    "paragraph": "paragraph",
    "strong": "strong",
    "subscript": "subscript",
    "superscript": "superscript",
    "time": "time",
    "term": "term",
    "code": "code",
    "math": "math",
    "note": "note",
    "generic": "generic",
    "none": "none",
    "presentation": "presentation",
}


def collect_geometric_nodes(page) -> list[dict]:
    """采集页面可交互元素（几何坐标 + 语义信息）

    在浏览器中注入 JS，遍历所有元素，提取可交互元素的坐标、尺寸、语义信息。
    返回 dict 列表，供 PageMap.build 使用。
    """
    if page is None:
        raise TypeError("page must not be None")
    # 等待页面稳定
    pm = PageMap(page.url)
    pm.wait_for_stable(page)
    return page.evaluate("""
        () => {
            const ARIA_ROLES = {
                'button':1,'link':1,'textbox':1,'searchbox':1,'heading':1,'checkbox':1,
                'radio':1,'switch':1,'tab':1,'menuitem':1,'combobox':1,'listbox':1,
                'option':1,'slider':1,'progressbar':1,'img':1,'navigation':1,'banner':1,
                'contentinfo':1,'form':1,'main':1,'complementary':1,'region':1,
                'separator':1,'tooltip':1,'dialog':1,'alert':1,'log':1,'status':1,
                'timer':1,'marquee':1,'application':1,'group':1,'tree':1,
                'treeitem':1,'grid':1,'gridcell':1,'row':1,'columnheader':1,
                'rowheader':1,'spacer':1,'menu':1,'menubar':1,'spinbutton':1,
                'scrollbar':1,'article':1,'document':1,'feed':1,'figure':1,
                'graphics-document':1,'graphics-object':1,'graphics-symbol':1,
                'meter':1,'blockquote':1,'caption':1,
            };
            const INTERACTIVE_TAGS = new Set([
                'a','button','input','select','textarea','video','audio',
                'details','summary','label','option','optgroup','fieldset',
                'legend','output','progress','meter'
            ]);
            const INTERACTIVE_ROLES = new Set([
                'button','link','textbox','searchbox','checkbox','radio',
                'switch','tab','menuitem','combobox','listbox','option',
                'slider','spinbutton','scrollbar','switch','gridcell',
                'treeitem','menu','menubar','row','rowheader','columnheader',
                'grid','tree','tablist','tabpanel','listbox','option',
                'menuitemcheckbox','menuitemradio','separator','toolbar',
                'tooltip','dialog','alert','alertdialog','log','status',
                'timer','marquee','application','progressbar','meter'
            ]);
            const CLICKABLE_CURSORS = new Set(['pointer', 'default']);

            function isVisible(el) {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }

            function getRole(el) {
                const explicit = el.getAttribute('role');
                if (explicit && ARIA_ROLES[explicit]) return explicit;
                const tag = el.tagName.toLowerCase();
                if (tag === 'button') return 'button';
                if (tag === 'a') return 'link';
                if (tag === 'input') {
                    const t = el.type || 'text';
                    if (t === 'checkbox') return 'checkbox';
                    if (t === 'radio') return 'radio';
                    if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
                    if (t === 'search') return 'searchbox';
                    return 'textbox';
                }
                if (tag === 'select') return 'combobox';
                if (tag === 'textarea') return 'textbox';
                if (tag === 'img') return 'img';
                if (/^h[1-6]$/.test(tag)) return 'heading';
                if (tag === 'nav') return 'navigation';
                if (tag === 'main') return 'main';
                if (tag === 'footer') return 'contentinfo';
                if (tag === 'header') return 'banner';
                if (tag === 'form') return 'form';
                if (tag === 'section') return 'region';
                if (tag === 'aside') return 'complementary';
                if (tag === 'details') return 'group';
                if (tag === 'summary') return 'button';
                if (tag === 'li') return 'listitem';
                if (tag === 'ul' || tag === 'ol') return 'list';
                if (tag === 'table') return 'table';
                if (tag === 'th') return 'columnheader';
                if (tag === 'tr') return 'row';
                if (tag === 'td') return 'cell';
                if (tag === 'video' || tag === 'audio') return 'application';
                return 'generic';
            }

            function getName(el) {
                const ariaLabel = el.getAttribute('aria-label') || '';
                if (ariaLabel.trim()) return ariaLabel.trim();
                const ariaLabelledBy = el.getAttribute('aria-labelledby');
                if (ariaLabelledBy) {
                    const labelEl = document.getElementById(ariaLabelledBy);
                    if (labelEl && labelEl.textContent) return labelEl.textContent.trim().substring(0, 100);
                }
                const title = el.getAttribute('title') || '';
                if (title.trim()) return title.trim();
                const placeholder = el.getAttribute('placeholder') || '';
                if (placeholder.trim()) return placeholder.trim();
                const alt = el.getAttribute('alt') || '';
                if (alt.trim()) return alt.trim();
                const text = (el.textContent || '').trim().substring(0, 100);
                if (text && text.length < 200) return text;
                // 兜底: 取 data-testid / id
                const testId = el.getAttribute('data-testid') || '';
                if (testId.trim()) return testId.trim();
                const id = el.getAttribute('id') || '';
                if (id.trim()) return '#' + id.trim();
                return '';
            }

            function isInteractive(el, role) {
                if (INTERACTIVE_TAGS.has(el.tagName.toLowerCase())) return true;
                if (INTERACTIVE_ROLES.has(role)) return true;
                if (el.hasAttribute('onclick') || el.hasAttribute('@click')) return true;
                if (el.getAttribute('tabindex') !== null) return true;
                return false;
            }

            const elements = document.querySelectorAll('*');
            const results = [];
            let id = 0;

            elements.forEach(el => {
                if (!isVisible(el)) return;
                const role = getRole(el);
                if (!isInteractive(el, role)) return;
                const name = getName(el);
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const tagName = el.tagName.toLowerCase();
                const isDisabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                const cursor = style.cursor;

                // 过滤：禁用态跳过
                if (isDisabled) return;

                // elementFromPoint 遮挡检测
                const centerX = rect.x + rect.width / 2;
                const centerY = rect.y + rect.height / 2;
                const topEl = document.elementFromPoint(centerX, centerY);
                const isOccluded = topEl !== el && !el.contains(topEl);

                // 尺寸阈值：< 5px 跳过
                if (rect.width < 5 || rect.height < 5) return;

                results.push({
                    id: 'geo-' + (id++),
                    role: role,
                    name: name,
                    tag: tagName,
                    disabled: isDisabled,
                    occluded: isOccluded,
                    cursor: cursor,
                    x: rect.x, y: rect.y, w: rect.width, h: rect.height,
                    styles: {
                        visible: true,
                    },
                    area: rect.width * rect.height,
                });
            });

            return results;
        }
    """)


class PageMap:
    """语义页面地图 — 管理页面可交互元素的几何/语义清单"""

    def __init__(self, url: str = ""):
        self.url: str = url
        self.nodes: dict[str, MapNode] = {}

    @classmethod
    def build(cls, page, url: str = "") -> PageMap:
        """从 Playwright page 构建 PageMap"""
        pm = cls(url=url)
        raw = collect_geometric_nodes(page)
        for d in raw:
            try:
                node = pm._dict_to_node(d)
                if node.id not in pm.nodes:
                    pm.nodes[node.id] = node
            except Exception:
                logger.debug("skip node", exc_info=True)
        logger.info("PageMap built: %d nodes", len(pm.nodes))
        return pm

    def query(
        self,
        role: str = None,
        name_contains: str = None,
        area_min: float = None,
        limit: int = 20,
    ) -> list[MapNode]:
        """按语义/几何条件查询节点列表"""
        candidates: list[MapNode] = []
        for node in self.nodes.values():
            if role and node.role.lower() != role.lower():
                continue
            if name_contains and name_contains.lower() not in node.name.lower():
                continue
            if area_min is not None and node.area < area_min:
                continue
            candidates.append(node)
        candidates.sort(key=lambda n: self.score_node(n), reverse=True)
        candidates = self._deduplicate(candidates)
        return candidates[:limit]

    def score_node(self, node: MapNode) -> float:
        """置信度评分（用于排序和兜底过滤）"""
        score = 0.0
        role = node.role.lower()

        if role in {"button", "link", "menuitem", "tab", "switch", "checkbox"}:
            score += 0.4
        elif role in {"textbox", "searchbox", "combobox", "listbox"}:
            score += 0.3

        action_kw = ["登录","注册","提交","发送","搜索","确认","取消","下一步","返回",
                     "关闭","下载","上传","保存","login","submit","send","search",
                     "confirm","cancel","next","back","close","download","upload","save"]
        name_lower = node.name.lower()
        if any(kw in name_lower for kw in action_kw):
            score += 0.3

        if node.width > 20 and node.height > 10:
            score += 0.1
        if not getattr(node, 'disabled', False):
            score += 0.05
        if not getattr(node, 'occluded', True):
            score += 0.05
        if 400 < node.area < 400000:
            score += 0.1

        return min(score, 1.0)

    def _deduplicate(self, nodes: list[MapNode], dist: float = 8) -> list[MapNode]:
        """去重：优先保留高分节点"""
        if len(nodes) <= 1:
            return nodes
        merged, taken = [], set()
        for i, n in enumerate(nodes):
            if i in taken: continue
            best = n
            for j in range(i+1, len(nodes)):
                if j in taken: continue
                if best.distance_to(nodes[j]) < dist:
                    if self.score_node(nodes[j]) > self.score_node(best):
                        best = nodes[j]
                    taken.add(j)
            merged.append(best)
        return merged

    def _dict_to_node(self, d: dict) -> MapNode:
        return MapNode(
            id=d.get("id", ""),
            role=d.get("role", "generic"),
            name=d.get("name", ""),
            x=float(d.get("x", 0)),
            y=float(d.get("y", 0)),
            w=float(d.get("w", 0)),
            h=float(d.get("h", 0)),
            styles=d.get("styles", {}),
            tag=d.get("tag", ""),
            disabled=bool(d.get("disabled", False)),
            occluded=bool(d.get("occluded", True)),
            cursor=d.get("cursor", ""),
        )

    def wait_for_stable(self, page, max_wait: float = 5.0) -> bool:
        """等待页面稳定（DOM 树不再变化）"""
        import time
        deadline = time.time() + max_wait
        last = len(page.evaluate("() => document.querySelectorAll('*').length"))
        while time.time() < deadline:
            time.sleep(0.5)
            cur = len(page.evaluate("() => document.querySelectorAll('*').length"))
            if cur == last:
                return True
            last = cur
        return False


def quick_find(page_map: PageMap, target_description: str) -> list[MapNode]:
    """快速从 PageMap 中按描述查找节点（role, name 格式）"""
    if not target_description or not target_description.strip():
        return []
    parts = [p.strip() for p in target_description.split(",")]
    role = parts[0] if parts else None
    name = parts[1] if len(parts) > 1 else None
    results = page_map.query(role=role, name_contains=name)

    # 模糊兜底
    if not results and name:
        name_lower = name.lower()
        for node in page_map.nodes.values():
            if name_lower in node.name.lower():
                results.append(node)
        results.sort(key=lambda n: page_map.score_node(n), reverse=True)

    results = page_map._deduplicate(results)
    return [n for n in results if page_map.score_node(n) >= 0.3][:10]