#!/bin/bash
# web-chat-bridge 演示脚本 — 多 Critic 并行评审
# 用法: bash examples/demo_multi_critic.sh

echo "============================================"
echo "  web-chat-bridge — 多 Critic 并行评审演示"
echo "============================================"
echo ""

# 创建示例文件
cat > /tmp/demo_code.py << 'EOF'
def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)

# BUG: 没有处理空列表的情况
# BUG: Python 2 整数除法问题
result = calculate_average([1, 2, 3, 4, 5])
print(result)
EOF

echo "📄 示例文件: /tmp/demo_code.py"
echo ""
cat /tmp/demo_code.py
echo ""
echo "============================================"
echo ""

# 启动 daemon（如果未运行）
echo "🔧 启动 daemon..."
python web_chat_bridge.py --serve --site deepseek &
DAEMON_PID=$!
sleep 3

# 多 Critic 并行评审
echo ""
echo "🎯 多 Critic 并行评审中..."
echo "   评审员: 豆包, Kimi, 通义千问"
echo ""
python scripts/multi_critic.py /tmp/demo_code.py --critics doubao,kimi,tongyi

# 自动迭代评审
echo ""
echo "============================================"
echo "🔄 自动迭代评审中..."
echo ""
python scripts/auto_review.py /tmp/demo_code.py --max-rounds 3

# 清理
kill $DAEMON_PID 2>/dev/null
echo ""
echo "============================================"
echo "  演示完成！"
echo "============================================"