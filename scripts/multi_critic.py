#!/usr/bin/env python3
"""
多 Critic 并行调度 + 加权投票 v1

同一份产出 → 同时发给 N 个 Critic → 并行评审 → 加权投票 → 综合判定。

用法：
    python scripts/multi_critic.py my_code.py
    python scripts/multi_critic.py my_code.py --critics doubao,kimi,tongyi
    python scripts/multi_critic.py my_code.py --context "关注内存安全"
    python scripts/multi_critic.py my_code.py --output result.json
"""

import subprocess
import json
import sys
import argparse
import concurrent.futures
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────
BRIDGE = Path(__file__).parent.parent / "web_chat_bridge.py"
DEFAULT_CRITICS = ["doubao", "kimi", "tongyi"]
TIMEOUT = 300  # 单个 Critic 超时（秒）


def review_single(content, critic, context=""):
    """单个 Critic 评审。在独立线程中运行。"""
    cmd = [
        "python", str(BRIDGE),
        "--review", content,
        "--site", critic,
        "--context", context,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        data = json.loads(proc.stdout)
        data["_critic"] = critic
        data["_status"] = "ok"
        return data
    except subprocess.TimeoutExpired:
        return {"_critic": critic, "_status": "timeout", "error": f"评审超时（{TIMEOUT}秒）"}
    except json.JSONDecodeError:
        return {"_critic": critic, "_status": "parse_error", "error": "无法解析评审结果"}
    except Exception as e:
        return {"_critic": critic, "_status": "error", "error": str(e)}


def multi_review(content, critics, context="", weights=None):
    """
    并行调用多个 Critic 评审同一内容。

    返回:
        {
            "verdict": "pass" | "warn" | "fail",
            "score": float,
            "votes": {"pass": int, "warn": int, "fail": int},
            "issues": [...],
            "suggestions": [...],
            "critic_results": {...},  # 每个 Critic 的原始结果
            "weights": {...}
        }
    """
    if weights is None:
        weights = {c: 1.0 for c in critics}

    results = {}

    # 并行调用所有 Critic
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(critics)) as executor:
        futures = {}
        for critic in critics:
            future = executor.submit(review_single, content, critic, context)
            futures[future] = critic

        for future in concurrent.futures.as_completed(futures):
            critic = futures[future]
            try:
                results[critic] = future.result()
                status = results[critic].get("_status", "?")
                if status == "ok":
                    v = results[critic].get("verdict", "?")
                    s = results[critic].get("score", "?")
                    print(f"  ✅ {critic}: {v.upper()} ({s}/10)")
                else:
                    print(f"  ❌ {critic}: {results[critic].get('error', status)}")
            except Exception as e:
                results[critic] = {"_critic": critic, "_status": "error", "error": str(e)}
                print(f"  ❌ {critic}: {e}")

    return _synthesize(results, weights)


def _synthesize(results, weights):
    """加权投票合成综合评审结果。"""
    total_weight = 0.0
    weighted_score = 0.0
    verdicts = []
    all_issues = []
    all_suggestions = []
    summaries = []

    for critic, r in results.items():
        if r.get("_status") != "ok":
            continue

        w = weights.get(critic, 1.0)
        score = r.get("score", 0)
        verdict = r.get("verdict", "fail")

        weighted_score += score * w
        total_weight += w
        verdicts.append(verdict)

        for issue in r.get("issues", []):
            # 去重：相同问题来自不同 Critic 只记一次
            if issue not in all_issues:
                all_issues.append(issue)

        for sug in r.get("suggestions", []):
            if sug not in all_suggestions:
                all_suggestions.append(sug)

        if r.get("summary"):
            summaries.append(f"[{critic}] {r['summary']}")

    if total_weight == 0:
        return {
            "verdict": "fail",
            "score": 0,
            "votes": {"pass": 0, "warn": 0, "fail": len(verdicts)},
            "issues": ["所有 Critic 评审失败"],
            "suggestions": [],
            "critic_results": results,
            "weights": weights,
        }

    final_score = round(weighted_score / total_weight, 1)

    # 投票统计
    pass_count = sum(1 for v in verdicts if v == "pass")
    warn_count = sum(1 for v in verdicts if v == "warn")
    fail_count = sum(1 for v in verdicts if v == "fail")

    # 多数票决定
    if pass_count > len(verdicts) / 2:
        final_verdict = "pass"
    elif fail_count > len(verdicts) / 2:
        final_verdict = "fail"
    elif warn_count > 0:
        final_verdict = "warn"
    else:
        # 平局：分数 >= 6 算 warn，< 6 算 fail
        final_verdict = "warn" if final_score >= 6 else "fail"

    return {
        "verdict": final_verdict,
        "score": final_score,
        "votes": {"pass": pass_count, "warn": warn_count, "fail": fail_count},
        "issues": all_issues,
        "suggestions": all_suggestions,
        "summary": " | ".join(summaries) if summaries else "",
        "critic_results": results,
        "weights": weights,
    }


def main():
    parser = argparse.ArgumentParser(
        description="多 Critic 并行评审 + 加权投票",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/multi_critic.py my_code.py
  python scripts/multi_critic.py my_code.py --critics doubao,kimi,tongyi
  python scripts/multi_critic.py my_code.py --context "嵌入式固件，关注内存安全"
  python scripts/multi_critic.py my_code.py --output result.json
        """,
    )
    parser.add_argument("file", help="要评审的文件路径")
    parser.add_argument(
        "--critics", default=",".join(DEFAULT_CRITICS),
        help=f"评审员列表，逗号分隔（默认 {','.join(DEFAULT_CRITICS)}）",
    )
    parser.add_argument("--context", default="", help="评审上下文说明")
    parser.add_argument("--weights", help="Critic 权重 JSON 文件路径（可选）")
    parser.add_argument("--output", "-o", help="输出 JSON 结果到文件（可选）")
    parser.add_argument("--json", action="store_true", help="仅输出 JSON，无人类可读日志")

    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        print(f"❌ 文件不存在: {args.file}", file=sys.stderr)
        sys.exit(2)

    content = filepath.read_text(encoding="utf-8")
    critics = [c.strip() for c in args.critics.split(",") if c.strip()]

    # 加载权重
    weights = None
    if args.weights:
        try:
            weights = json.loads(Path(args.weights).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 无法加载权重文件: {e}，使用默认等权", file=sys.stderr)

    if not args.json:
        print(f"🎯 多 Critic 并行评审")
        print(f"📄 文件: {args.file} ({len(content)} 字符)")
        print(f"🔍 评审员: {', '.join(critics)}")
        if weights:
            print(f"⚖️ 权重: {json.dumps(weights, ensure_ascii=False)}")
        if args.context:
            print(f"📋 上下文: {args.context}")
        print()

    result = multi_review(content, critics, args.context, weights)

    if not args.json:
        print(f"\n{'='*50}")
        print(f"📊 综合判定: {result['verdict'].upper()}")
        print(f"📈 加权评分: {result['score']}/10")
        print(f"🗳️ 投票: pass={result['votes']['pass']} "
              f"warn={result['votes']['warn']} "
              f"fail={result['votes']['fail']}")
        if result.get("issues"):
            print(f"\n🔴 问题汇总 ({len(result['issues'])} 条):")
            for i, issue in enumerate(result["issues"][:10]):
                print(f"   {i+1}. {issue}")
        if result.get("suggestions"):
            print(f"\n💡 改进建议 ({len(result['suggestions'])} 条):")
            for i, sug in enumerate(result["suggestions"][:5]):
                print(f"   {i+1}. {sug}")

    output = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        if not args.json:
            print(f"\n📝 结果已保存: {args.output}")
    elif args.json:
        print(output)

    # 退出码
    if result["verdict"] == "pass":
        sys.exit(0)
    elif result["verdict"] == "warn":
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()