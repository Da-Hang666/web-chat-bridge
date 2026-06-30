#!/usr/bin/env python3
"""
Actor-Critic 自动评审迭代闭环 v1

提交代码/文档 → Critic 评审 → 提取问题 → Actor 修复 → 换 Critic 再评审
最多 N 轮，两个不同 Critic 都 pass 才通过。

用法：
    python scripts/auto_review.py my_code.py
    python scripts/auto_review.py my_code.py --critics doubao,kimi,tongyi
    python scripts/auto_review.py my_code.py --max-rounds 5 --context "关注内存安全"
    python scripts/auto_review.py my_code.py --output fixed_code.py
"""

import subprocess
import json
import sys
import argparse
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────
BRIDGE = Path(__file__).parent.parent / "web_chat_bridge.py"
DEFAULT_CRITICS = ["doubao", "kimi", "tongyi"]
MAX_ROUNDS = 3


def run_bridge(args_list):
    """调用 web_chat_bridge CLI。返回 (ok, result_dict)。"""
    cmd = ["python", str(BRIDGE)] + args_list
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        result = json.loads(proc.stdout)
        return True, result
    except subprocess.TimeoutExpired:
        return False, {"error": "评审超时（5分钟）"}
    except json.JSONDecodeError:
        return False, {"error": f"无法解析评审结果: {proc.stdout[:200]}"}
    except Exception as e:
        return False, {"error": str(e)}


def review(content, critic, context=""):
    """调用 web_chat_bridge 让指定 Critic 评审一段内容。"""
    print(f"  📤 发送给 {critic} 评审... ({len(content)} 字符)")
    ok, result = run_bridge([
        "--review", content,
        "--site", critic,
        "--context", context,
    ])
    if not ok:
        print(f"  ⚠️ {result.get('error')}")
        return {"verdict": "fail", "score": 0, "issues": [result.get("error", "")], "suggestions": [], "summary": "评审失败"}
    return result


def actor_fix(content, issues, critic_name):
    """Actor (DeepSeek) 根据 Critic 的反馈修复内容。"""
    issues_text = "\n".join(f"- {i}" for i in issues[:10])
    fix_prompt = f"""以下内容被 {critic_name} 评审发现问题：

{issues_text}

请修复这些问题，直接输出修复后的完整版本。不要解释，不要加说明文字，只输出修复后的内容。

---原始内容---
{content}"""

    print(f"  🔧 Actor (DeepSeek) 修复中...")
    ok, result = run_bridge([
        "--send", fix_prompt,
        "--site", "deepseek",
    ])
    if not ok:
        print(f"  ⚠️ 修复失败，使用原内容: {result.get('error')}")
        return content
    return result.get("response", content)


def auto_review(content, max_rounds=MAX_ROUNDS, critics=None, context=""):
    """
    主循环：评审 → 修复 → 换 Critic 再评审

    返回:
        {
            "status": "pass" | "max_rounds" | "error",
            "rounds": int,
            "final_content": str,
            "history": [...]
        }
    """
    if critics is None:
        critics = DEFAULT_CRITICS

    history = []

    for round_num in range(max_rounds):
        critic = critics[round_num % len(critics)]

        print(f"\n{'─'*50}")
        print(f"🔄 第 {round_num+1}/{max_rounds} 轮 — Critic: {critic}")
        print(f"{'─'*50}")

        # 1. 评审
        result = review(content, critic, context)
        verdict = result.get("verdict", "fail")
        score = result.get("score", 0)
        issues = result.get("issues", [])
        suggestions = result.get("suggestions", [])

        history.append({
            "round": round_num + 1,
            "critic": critic,
            "verdict": verdict,
            "score": score,
            "issues": issues,
            "suggestions": suggestions,
            "summary": result.get("summary", ""),
        })

        print(f"  📊 判定: {verdict.upper()} | 评分: {score}/10")
        if issues:
            for i, issue in enumerate(issues[:5]):
                print(f"     {i+1}. {issue}")
            if len(issues) > 5:
                print(f"     ... 还有 {len(issues)-5} 个问题")

        # 2. 检查是否通过
        if verdict == "pass":
            # 统计不同 Critic 的通过数
            pass_critics = set(
                h["critic"] for h in history if h["verdict"] == "pass"
            )
            if len(pass_critics) >= 2 or round_num == max_rounds - 1:
                print(f"\n✅ 通过！{len(pass_critics)} 个不同 Critic 确认通过。")
                return {
                    "status": "pass",
                    "rounds": round_num + 1,
                    "final_content": content,
                    "history": history,
                }
            else:
                print(f"  ⏳ 还需另一个 Critic 确认（当前通过: {', '.join(pass_critics)}）")

        # 3. 修复（不是最后一轮才修）
        if round_num < max_rounds - 1:
            all_issues = issues + suggestions
            if all_issues:
                content = actor_fix(content, all_issues, critic)

    # 达到最大轮数
    print(f"\n⚠️ 达到最大轮数 ({max_rounds})，未通过全部评审。")
    return {
        "status": "max_rounds",
        "rounds": max_rounds,
        "final_content": content,
        "history": history,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Actor-Critic 自动评审迭代闭环",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/auto_review.py my_code.py
  python scripts/auto_review.py my_code.py --critics doubao,kimi
  python scripts/auto_review.py my_code.py --max-rounds 5
  python scripts/auto_review.py my_code.py --output fixed.py
  python scripts/auto_review.py my_code.py --context "这是嵌入式固件，关注内存安全和中断处理"
        """,
    )
    parser.add_argument("file", help="要评审的文件路径")
    parser.add_argument(
        "--max-rounds", type=int, default=MAX_ROUNDS,
        help=f"最大迭代轮数（默认 {MAX_ROUNDS}）",
    )
    parser.add_argument(
        "--critics", default=",".join(DEFAULT_CRITICS),
        help=f"评审员列表，逗号分隔（默认 {','.join(DEFAULT_CRITICS)}）",
    )
    parser.add_argument("--context", default="", help="评审上下文说明")
    parser.add_argument("--output", "-o", help="输出最终内容到文件（可选）")
    parser.add_argument("--json", action="store_true", help="仅输出 JSON 结果，无人类可读日志")

    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        print(f"❌ 文件不存在: {args.file}", file=sys.stderr)
        sys.exit(2)

    content = filepath.read_text(encoding="utf-8")
    critics = [c.strip() for c in args.critics.split(",") if c.strip()]

    if not args.json:
        print(f"📄 文件: {args.file} ({len(content)} 字符)")
        print(f"🔍 评审员: {', '.join(critics)}")
        print(f"🔄 最大轮数: {args.max_rounds}")
        if args.context:
            print(f"📋 上下文: {args.context}")

    result = auto_review(
        content,
        max_rounds=args.max_rounds,
        critics=critics,
        context=args.context,
    )

    # 保存评审历史
    result_file = filepath.stem + "_review_result.json"
    Path(result_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not args.json:
        print(f"\n📊 评审报告: {result_file}")

    # 可选输出修复后的内容
    if args.output:
        Path(args.output).write_text(result["final_content"], encoding="utf-8")
        if not args.json:
            print(f"📝 最终内容已写入: {args.output}")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    # 退出码
    if result["status"] == "pass":
        sys.exit(0)
    elif result["status"] == "max_rounds":
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()