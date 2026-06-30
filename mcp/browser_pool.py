"""
浏览器常驻池 — 预热 Edge/CDP 实例，维持登录态，任务时直接取用。
"""

import socket
import sys
import threading
import time
from config import POOL_SIZE, POOL_IDLE_TTL, SITE_CONFIGS
from browser_controller import connect_via_cdp, find_free_cdp_port, launch_edge_with_cdp, wait_for_cdp
from page_map import PageMap, MapNode, collect_geometric_nodes


class BrowserInstance:
    """单个浏览器实例的封装。"""
    def __init__(self, cdp_port: int, target_url: str, site_id: str = "deepseek"):
        self.cdp_port = cdp_port
        self.target_url = target_url
        self.site_id = site_id
        self.playwright = None
        self.browser = None
        self.page = None
        self.in_use = False
        self.last_used = 0.0
        self.healthy = False
        self.page_map: PageMap | None = None

    def is_alive(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False


class BrowserPool:
    """管理多个预热的浏览器实例。"""
    
    def __init__(self, size: int = 2, site: str = "deepseek"):
        self.size = size
        self.site = site
        self.target_url = SITE_CONFIGS[site]["url"] if site in SITE_CONFIGS else "https://chat.deepseek.com"
        self.instances: list[BrowserInstance] = []
        self._lock = threading.Lock()
        self._heartbeat_thread = None
        self._running = False
        self._cdp_ports = []  # 分配的端口列表

    def start(self):
        """启动池子：预启动 N 个浏览器实例。"""
        self._running = True
        # 自动分配空闲 CDP 端口
        ports = _allocate_cdp_ports(self.size)
        self._cdp_ports = ports
        
        for port in ports:
            inst = BrowserInstance(port, self.target_url, self.site)
            self._init_instance(inst)
            self.instances.append(inst)
        
        # 启动心跳维护线程
        self._heartbeat_thread = threading.Thread(target=self._maintenance_loop, daemon=True)
        self._heartbeat_thread.start()
        
        healthy = sum(1 for i in self.instances if i.healthy)
        sys.stderr.write(f"[Pool] 浏览器池就绪: {healthy}/{self.size} 实例健康\n")

    def acquire(self, timeout: float = 30.0) -> BrowserInstance | None:
        """获取一个空闲实例。阻塞直到有空闲或超时。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for inst in self.instances:
                    if not inst.in_use and inst.healthy:
                        inst.in_use = True
                        inst.last_used = time.time()
                        return inst
            time.sleep(0.5)
        return None

    def release(self, inst: BrowserInstance):
        """归还实例到池子。"""
        with self._lock:
            inst.in_use = False
            inst.last_used = time.time()

    def _init_instance(self, inst: BrowserInstance):
        """初始化单个实例：启动 Edge + CDP 连接 + 导航。"""
        port = inst.cdp_port
        if not _is_cdp_listening(port):
            launch_edge_with_cdp(port)
            wait_for_cdp(port, timeout=30)
        
        try:
            inst.playwright, inst.browser, inst.page = connect_via_cdp(port)
            # 导航到目标页面
            if inst.target_url not in inst.page.url:
                inst.page.goto(inst.target_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
            inst.healthy = inst.target_url in inst.page.url
            if inst.healthy:
                sys.stderr.write(f"[Pool] 实例 {port} 就绪: {inst.page.url}\n")
                # 建图
                try:
                    geom_nodes = collect_geometric_nodes(inst.page)
                    inst.page_map = PageMap(inst.page.url)
                    for gn in geom_nodes:
                        node = MapNode(**gn)
                        inst.page_map.add_node(node)
                    sys.stderr.write(f"[Pool] 实例 {port} 地图已构建: {len(inst.page_map.nodes)} 节点\n")
                except Exception as e:
                    sys.stderr.write(f"[Pool] 实例 {port} 建图失败: {e}\n")
        except Exception as e:
            sys.stderr.write(f"[Pool] 实例 {port} 初始化失败: {e}\n")
            inst.healthy = False

    def _maintenance_loop(self):
        """后台维护：心跳检测 + 不健康实例自动修复。"""
        while self._running:
            time.sleep(15)
            with self._lock:
                for inst in self.instances:
                    if inst.in_use:
                        continue
                    if not inst.is_alive():
                        sys.stderr.write(f"[Pool] 实例 {inst.cdp_port} 不健康，重新初始化\n")
                        try:
                            self._init_instance(inst)
                        except Exception as e:
                            sys.stderr.write(f"[Pool] 实例 {inst.cdp_port} 修复失败: {e}\n")


def _allocate_cdp_ports(count: int, start: int = 9222) -> list[int]:
    """分配 N 个空闲 CDP 端口。"""
    ports = []
    current = start
    for _ in range(count):
        port = find_free_cdp_port(current, max_attempts=20)
        if port is None:
            raise RuntimeError(f"无法分配 {count} 个 CDP 端口")
        ports.append(port)
        current = port + 1
    return ports


def _is_cdp_listening(port: int) -> bool:
    try:
        s = socket.create_connection(('127.0.0.1', port), timeout=2)
        s.close()
        return True
    except Exception:
        return False


# 全局池子引用（daemon 启动时设置）
_pool: BrowserPool | None = None

def get_pool() -> BrowserPool | None:
    return _pool

def set_pool(pool: BrowserPool):
    global _pool
    _pool = pool