# web-chat-bridge vs browser-use — 详细对比

## 一句话总结

- **browser-use**: 通用 AI 浏览器 Agent，"帮我订机票"
- **web-chat-bridge**: Actor-Critic 代码评审工厂，"帮我审代码"

## 功能对比

| 维度 | browser-use | web-chat-bridge |
|------|:-----------:|:---------------:|
| **定位** | 通用任务自动化 | 多模型代码评审 + 迭代 |
| **LLM 接入** | API Key（OpenAI/Anthropic/...） | 网页 UI（零成本） |
| **支持的模型** | 有 API 的模型 | 所有有网页版的模型（含豆包/Kimi/通义） |
| **多模型协同** | ❌ | ✅ Actor-Critic, 3+ 模型 |
| **评审→修复→再评审** | ❌ | ✅ auto_review.py 自动闭环 |
| **并行多 Critic** | ❌ | ✅ multi_critic.py 加权投票 |
| **人类化输入** | ❌ | ✅ 随机延迟 + 逐字打字 |
| **评审缓存** | ❌ | ✅ L1+L2 两级缓存 |
| **浏览器操作** | ✅ | ✅ (v5 Browser Agent) |
| **CAPTCHA 绕过** | ✅ (Cloud) | ❌ (规划中) |
| **Cloud 托管** | ✅ | ❌ |
| **开源许可** | MIT | MIT |

## 架构对比

### browser-use

```
用户任务 → Agent 循环 → LLM 决策 → Playwright 执行
                          ↑
                     API 调用（需要 Key）
```

### web-chat-bridge

```
用户代码 → Daemon → Playwright → 网页大模型 UI
              ↑                      ↓
         HTTP :19999             评审结果
              ↑                      ↓
      auto_review.py ←── 自动迭代 ←──┘
      multi_critic.py ←── 并行投票 ←──┘
```

## 适用场景

### 用 browser-use 如果：

- 需要操作各种不同网站（信息检索、订票、填表）
- 有 API Key 预算
- 需要 Cloud 托管的隐身浏览器

### 用 web-chat-bridge 如果：

- 做代码/文档评审，需要多个模型交叉验证
- 不想付 API 费用（或者模型没有 API）
- 需要自动迭代修复闭环
- 需要 DeepSeek + 豆包 + Kimi 三模型协同

## 可以互补

web-chat-bridge 的评审能力 + browser-use 的通用操作能力可以组合：

1. browser-use 操作浏览器完成任务
2. web-chat-bridge 评审 browser-use 的产出
3. browser-use 根据评审结果修复