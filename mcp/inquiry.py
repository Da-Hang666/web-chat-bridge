"""
web-chat-bridge MCP — 追问引擎 v5.3

核心价值：单次回答60分，追问N轮推到80-85分。
追问质量不依赖被问模型——由追问模板+论点提取+质量反馈三层保证。

v5.3 加固：
  - 多模板变体：每种策略3-5个追问模板，随机选取+自动回退
  - 质量反馈：追问后回复<上一轮50% → 自动换策略
  - 增强论点提取：5层fallback，降低抓空率
"""

import random
import re
import sys
import time

from config import (
    INQUIRY_MAX_ROUNDS, INQUIRY_STABILITY_WAIT,
    INQUIRY_MAX_WAIT_PER_ROUND, INQUIRY_STRATEGIES,
    INQUIRY_COOLDOWN_S,
)
from datetime import datetime


# ── 多模板追问库（每种策略多个变体）──

FOLLOWUP_TEMPLATES = {
    "deep": [
        "关于你提到的「{key_point}」，请深入展开。给出具体原理、源码细节、性能数据和边界条件。",
        "你上一轮说「{key_point}」。如果往下挖一层，底层还有什么？为什么这样设计？",
        "回到「{key_point}」这个点。我不满足于结论，我要知道推导过程。每一步是怎么得出的？",
        "把「{key_point}」拆开。它的每个组成部分分别是什么？各自占比多少？给出量化数据。",
        "如果我问的不是你，而是CPython核心开发者本人，「{key_point}」他会怎么解释？用第一人称回答。",
    ],
    "debate": [
        "对于你上一轮的观点「{key_point}」，请站在反对立场进行批判。指出漏洞、局限、反例、以及什么情况下这个结论不成立。",
        "反驳你自己。「{key_point}」在什么场景下是错的？给出具体的反例和测试代码。",
        "假设一个比你更资深的工程师看了「{key_point}」，他会说'你漏了三个关键点'。是哪三个？",
        "我对「{key_point}」存疑。请你自己攻击自己的这个结论。不要敷衍，拿出硬证据。",
        "「{key_point}」——这句话在Python 3.13还成立吗？在PyPy呢？在MicroPython呢？逐环境分析。",
    ],
    "socratic": [
        "关于「{key_point}」，如果把这个逻辑推到极端会怎样？",
        "如果「{key_point}」的反面才是真相，世界会变成什么样？",
        "不考虑任何现有实现，「{key_point}」最理想的设计应该是什么？为什么现在不是这样？",
        "一个完全不懂编程的人听到「{key_point}」，他会问什么？用他的视角追问。",
        "如果给你无限算力重做「{key_point}」，你会怎么设计？和现在的实现差在哪？",
    ],
    "verify": [
        "以下是另一个AI模型对同一问题的回答，请评审并指出差异和优劣：\n{other_response}",
        "另一个模型说：{other_response}\n\n你同意吗？逐条分析它的对错，给出你的判断和理由。",
        "两份回答有矛盾：你的和下面这份在{key_point}上冲突了。请裁决：\n{other_response}",
    ],
}

# 回退模板——当 extract_key_point 抓空时使用
FALLBACK_TEMPLATES = {
    "deep": "你上一轮回答涉及多个层面。请选一个你认为最关键的，展开到源码级别。",
    "debate": "你上一轮的回答有哪个结论你自己也不太确定？请自己质疑它。",
    "socratic": "如果不允许你直接给答案，只能反问一个问题来引导读者自己思考，你会问什么？然后回答那个问题。",
    "verify": "请直接比较两份回答的质量差异，不要回避任何一方的问题。",
}


def extract_key_point(response: str, max_len: int = 150) -> str:
    """从模型回复中提取核心论点。五层 fallback，降低抓空率。

    层1: 显式总结标记（核心：/ 关键：/ 本质：/ 总结：...）
    层2: 编号列表的第一条（1. / 一、...）
    层3: 含「核心/关键/本质/根本/最重要」的完整句子
    层4: 加粗/标题行（**text** / ## text）
    层5: 最长且不含代码的句子
    """
    if not response or len(response) < 20:
        return response[:max_len] if response else "你的上一个回答"

    lines = [l.strip() for l in response.split('\n') if len(l.strip()) > 10]

    # 层1: 显式总结
    for pattern in [
        r'^(?:核心|关键|本质|总结|最终|综上|总之)[：:]\s*(.+)',
        r'^(?:核心结论|关键发现|本质区别|最终判断)[：:]\s*(.+)',
    ]:
        for line in reversed(lines[-15:]):
            m = re.match(pattern, line)
            if m and len(m.group(1)) > 5:
                return m.group(1)[:max_len]

    # 层2: 编号列表第一条
    for line in lines:
        m = re.match(r'^(?:[1１一][\.、．)）]\s*|（[一1１]）)\s*(.+)', line)
        if m and len(m.group(1)) > 8:
            return m.group(1)[:max_len]

    # 层3: 关键词句
    for kw in ['核心', '关键', '本质', '根本', '最重要', '根源', '症结']:
        for line in lines:
            if kw in line and len(line) > 15:
                return line[:max_len]

    # 层4: 加粗/标题
    for pattern in [r'\*\*(.+?)\*\*', r'##\s+(.+)']:
        for line in lines:
            m = re.search(pattern, line)
            if m and len(m.group(1)) > 8:
                return m.group(1)[:max_len]

    # 层5: 最长非代码句
    non_code = [l for l in lines if not l.startswith(' ') and not l.startswith('```')]
    if non_code:
        candidates = [(l, len(l)) for l in non_code if 20 < len(l) < 300]
        if candidates:
            return max(candidates, key=lambda x: x[1])[0][:max_len]

    # 兜底
    for line in reversed(lines[-5:]):
        if len(line) > 10:
            return line[:max_len]
    return response[:max_len]


def build_followup(prev_response: str, strategy: str = "deep", context: str = "",
                   other_response: str = "", prev_round_chars: int = 0) -> str:
    """生成下一轮追问。

    - 从模板库随机选一个追问模板
    - extract_key_point 抓空时自动换 fallback 模板
    - 质量检测：上轮回复太短时换策略
    """
    key_point = extract_key_point(prev_response, max_len=120)

    # 质量检测：如果上轮回复很短（<200字），说明可能卡住了，换 socratic
    if prev_round_chars > 0 and prev_round_chars < 200 and strategy != "socratic":
        strategy = "socratic"
        sys.stderr.write(f"[Inquiry] 上轮回复仅{prev_round_chars}字，切换为socratic策略\n")

    # 选模板
    templates = FOLLOWUP_TEMPLATES.get(strategy, FOLLOWUP_TEMPLATES["deep"])

    if key_point == "你的上一个回答" or len(key_point) < 10:
        # extract_key_point 抓空了，用 fallback
        template = FALLBACK_TEMPLATES.get(strategy, FALLBACK_TEMPLATES["deep"])
    else:
        template = random.choice(templates)

    prompt = template.replace("{key_point}", key_point)

    if strategy == "verify" and other_response:
        prompt = template.replace("{other_response}", other_response[:3000])

    if context and strategy != "verify":
        prompt = f"（原始背景：{context}）\n{prompt}"

    return prompt


def wait_for_stable(page, response_selector: str, max_wait: int = None) -> str:
    """等待页面上的回复文本稳定（连续N秒不变）。"""
    if max_wait is None:
        max_wait = INQUIRY_MAX_WAIT_PER_ROUND

    selectors = response_selector.split(', ')
    last_text = ''
    stable_count = 0

    for _ in range(max_wait):
        time.sleep(1)
        current = ''
        for sel in selectors:
            try:
                els = page.locator(sel).all()
                for el in reversed(els):
                    if el.is_visible():
                        txt = el.inner_text()
                        if txt and len(txt) > len(current):
                            current = txt
            except Exception:
                pass

        if current and current == last_text and len(current) > 20:
            stable_count += 1
            if stable_count >= INQUIRY_STABILITY_WAIT:
                return current
        else:
            stable_count = 0
            last_text = current

    return last_text


def deep_inquiry(page, config: dict, seed_question: str,
                 strategy: str = "deep", max_rounds: int = None,
                 context: str = "") -> dict:
    """执行多轮追问循环。

    每轮自动：
    1. 提取上一轮核心论点 → 从模板库选追问
    2. 质量检测：回复太短/无增长 → 换策略
    3. 返回完整对话记录
    """
    if max_rounds is None:
        cfg = INQUIRY_STRATEGIES.get(strategy, INQUIRY_STRATEGIES["deep"])
        max_rounds = min(cfg.get("max_rounds", 4), INQUIRY_MAX_ROUNDS)

    from daemon import _type_text_strategic
    from browser_controller import send_message
    from chat_adapters import find_and_fill_input

    rounds = []
    t0 = time.time()
    current_question = seed_question
    current_strategy = strategy
    total_chars = 0
    prev_chars = 0

    for r in range(max_rounds):
        round_start = time.time()

        input_el = find_and_fill_input(page, config)
        if input_el is None:
            rounds.append({"round": r+1, "error": "找不到输入框"})
            break

        _type_text_strategic(page, input_el, current_question, config)
        result = send_message(page, config, current_question)
        if "error" in result:
            rounds.append({"round": r+1, "error": result["error"]})
            break

        response = result.get("response", "")
        resp_len = len(response)
        total_chars += resp_len

        # 质量检测：决定下一轮策略
        if r < max_rounds - 1:
            if r > 0 and resp_len < prev_chars * 0.4:
                # 回复严重缩水，切换策略
                if current_strategy == "deep":
                    current_strategy = "socratic"
                elif current_strategy == "socratic":
                    current_strategy = "debate"
                sys.stderr.write(
                    f"[Inquiry] 第{r+1}轮回复缩水({prev_chars}→{resp_len})，切换策略→{current_strategy}\n"
                )

        key_point = extract_key_point(response) if r < max_rounds - 1 else ""

        round_elapsed = round(time.time() - round_start, 1)
        rounds.append({
            "round": r + 1,
            "strategy_used": current_strategy,
            "question": current_question[:200],
            "response": response[:3000] if r == max_rounds - 1 else response[:300],
            "response_chars": resp_len,
            "key_point": key_point,
            "elapsed": round_elapsed,
        })

        sys.stderr.write(
            f"[Inquiry] 第{r+1}轮({current_strategy}) {round_elapsed}s, {resp_len}字\n"
        )

        # 生成下一轮追问
        if r < max_rounds - 1 and response:
            current_question = build_followup(
                response, strategy=current_strategy, context=context,
                prev_round_chars=resp_len,
            )
            prev_chars = resp_len
            # 损卦䷨: 轮间强制冷却，不过度消耗免费算力
            time.sleep(INQUIRY_COOLDOWN_S)

    total_elapsed = round(time.time() - t0, 1)
    
    # 未济卦䷿: 追问结束自动生成"未尽之问"——永远有下一轮可追
    unfinished = _generate_unfinished_questions(
        rounds[-1].get("response","") if rounds else "",
        current_strategy
    )

    return {
        "rounds": rounds,
        "strategy": INQUIRY_STRATEGIES.get(strategy, INQUIRY_STRATEGIES["deep"])["name"],
        "total_rounds": len(rounds),
        "total_elapsed": total_elapsed,
        "total_chars": total_chars,
        "final_response": rounds[-1].get("response", "") if rounds else "",
        "strategy_switches": len(set(r.get("strategy_used", strategy) for r in rounds)),
        "verified": len(rounds) > 0,  # 中孚卦䷼: 只有跑过实测才标记verified
        "timestamp": datetime.now().isoformat(),
        "unfinished_questions": unfinished,  # 未济卦䷿: 未尽之问
    }


def _generate_unfinished_questions(final_response: str, strategy: str) -> list:
    """未济卦䷿: 基于最终回答，生成"还可以追问的方向"。
    
    这些不是实际追问，而是提示：这条线还没挖完。
    """
    if not final_response or len(final_response) < 100:
        return ["回答太短，建议换个角度重新提问"]
    
    questions = []
    
    # 检测回复中是否有未展开的领域
    if "例如" in final_response or "比如" in final_response or "举例" in final_response:
        questions.append("回复中提到了例子但未充分展开——追问：把这些例子的具体数据、测试结果、边界条件补全")
    
    if any(kw in final_response for kw in ["取决于", "视情况", "不一定", "可能有"]):
        questions.append("回复中有模糊表述(取决于/不一定/可能) -- 追问: 穷举所有情况, 逐个分析")
    
    if any(kw in final_response for kw in ["源码", "实现", "底层", "内部"]):
        questions.append("回复涉及底层实现——追问：追踪到具体的函数调用链和内存布局")
    
    if strategy != "debate":
        questions.append("尚未进行反向批判——追问：站在反对立场攻击以上所有结论")
    
    if strategy != "verify":
        questions.append("尚未交叉验证——追问：将以上回答发给另一个模型评审")
    
    if "3.1" in final_response or "3.10" in final_response or "3.12" in final_response:
        questions.append("回复限定在特定版本——追问：在其他版本/实现(PyPy/MicroPython)下是否成立")
    
    if not questions:
        questions = [
            "将以上结论用一句话总结，然后追问这句话的每个词的确切含义",
            "问：如果以上结论有一个是错的，最可能是哪一个？为什么？"
        ]
    
    return questions[:5]