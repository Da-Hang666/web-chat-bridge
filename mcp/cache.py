"""
web-chat-bridge MCP — 缓存系统

从 v3 提取的独立缓存模块：
- _normalize_context — 上下文语义归一化
- _normalize_content — 内容规范化
- _cache_key — 构建缓存 key (L1 精确 / L2 宽松)
- TTLCache + 持久化 (JSON Lines)
- singleflight 合并并发请求
- 缓存统计
"""

import hashlib
import json
import re
import sys
import time
import threading
from pathlib import Path
from cachetools import TTLCache

from config import (
    CACHE_MAXSIZE, CACHE_TTL, CACHE_MAX_AGE,
    CACHE_SAVE_INTERVAL, CACHE_FILE,
)


# ── 全局状态 ──

_review_cache = TTLCache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL)
_cache_birth = {}          # key → 首次写入时间戳
_cache_dirty = False
_cache_last_save = 0.0

# 缓存统计
_cache_stats = {"total": 0, "hit_l1": 0, "hit_l2": 0, "miss": 0}

# singleflight 合并并发
_inflight = {}
_inflight_lock = threading.Lock()


# ── 上下文 / 内容归一化 ──

def normalize_context(ctx: str) -> str:
    """上下文语义归一化：去程度副词，'关注内存安全'和'重点关注内存安全'→同一key。"""
    if not ctx:
        return ""
    for w in ['重点', '特别', '非常', '很', '极其', '十分', '请', '务必', '一定', '尽量', '麻烦']:
        ctx = ctx.replace(w, '')
    return ctx.strip()


def normalize_content(content: str) -> str:
    """规范化内容：strip前后空白、统一换行符、压缩连续空行。
    让微小格式差异不影响缓存命中。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def cache_key(content: str, context: str = "", mode: str = "expert", do_normalize: bool = True) -> str:
    """构建缓存 key。默认规范化 content 和 context。"""
    if do_normalize:
        content = normalize_content(content)
        context = normalize_context(context) if context else ""
    raw = json.dumps({"c": content, "ctx": context, "m": mode}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# ── 持久化 ──

def load_cache():
    """从磁盘恢复缓存。"""
    if not CACHE_FILE.exists():
        return
    try:
        count = 0
        for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                key = entry.get("k", "")
                value = entry.get("v")
                expire = entry.get("e", 0)
                if key and value is not None:
                    if expire > time.time() or expire == 0:
                        _review_cache[key] = value
                        count += 1
            except (json.JSONDecodeError, KeyError):
                continue
        if count:
            sys.stderr.write(f"[Cache] 从磁盘恢复 {count} 条缓存\n")
    except Exception:
        pass


def save_cache():
    """将缓存落盘为 JSON Lines 文件。"""
    global _cache_dirty, _cache_last_save
    try:
        lines = []
        now = time.time()
        for key, value in _review_cache.items():
            expire = now + CACHE_TTL
            lines.append(json.dumps({"k": key, "v": value, "e": expire}, ensure_ascii=False))
        CACHE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _cache_dirty = False
        _cache_last_save = now
    except Exception:
        pass


def mark_cache_dirty():
    """标记缓存脏，触发延迟落盘。"""
    global _cache_dirty
    _cache_dirty = True


def maybe_save_cache():
    """如果缓存脏且超过落盘间隔，自动保存。"""
    global _cache_dirty, _cache_last_save
    if _cache_dirty and (time.time() - _cache_last_save) > CACHE_SAVE_INTERVAL:
        save_cache()


def flush_cache():
    """强制落盘。"""
    save_cache()
    sys.stderr.write("[Cache] 已落盘\n")


# ── 带 singleflight 的缓存读取 ──

def with_cache(key: str, compute_fn, force_refresh: bool = False, level: str = "L1"):
    """缓存装饰器：singleflight合并 + 分级key + 命中率统计。"""
    global _cache_stats
    _cache_stats["total"] += 1

    # 缓存命中快速路径（含滑动 TTL）
    if not force_refresh and key in _review_cache:
        if key in _cache_birth and (time.time() - _cache_birth[key]) > CACHE_MAX_AGE:
            del _review_cache[key]
            _cache_birth.pop(key, None)
        else:
            # 滑动 TTL：命中时 pop + reinsert 重置过期计时
            value = _review_cache.pop(key)
            _review_cache[key] = value
            _cache_stats[f"hit_{level.lower()}"] += 1
            return _review_cache[key], True

    # singleflight：同 key 并发只跑一次 compute_fn
    with _inflight_lock:
        if key not in _inflight:
            _inflight[key] = threading.Event()
            is_leader = True
        else:
            is_leader = False
            event = _inflight[key]

    if not is_leader:
        event.wait()
        if key in _review_cache:
            _cache_stats[f"hit_{level.lower()}"] += 1
            return _review_cache[key], True

    # leader 执行
    try:
        _cache_stats["miss"] += 1
        result = compute_fn()
        _review_cache[key] = result
        _cache_birth[key] = time.time()
        mark_cache_dirty()
        maybe_save_cache()
        return result, False
    finally:
        if is_leader:
            with _inflight_lock:
                event = _inflight.pop(key, None)
                if event:
                    event.set()


# ── 直接缓存操作（供 daemon HTTP 使用）──

def get_cache():
    return _review_cache

def set_cache(key, value):
    _review_cache[key] = value

def clear_cache():
    _review_cache.clear()

def get_cache_stats():
    return dict(_cache_stats)

def get_cache_size():
    return len(_review_cache)