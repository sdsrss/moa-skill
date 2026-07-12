✅点击上方🔺公众号🔺关注我✅  

看到Nous Research 发出的消息时，我愣了一下。

他们说最强大的模型都被 "门禁" 了——只有少数人能访问。但 Hermes Agent 的 MoA 2.0（Mixture of Agents）让组合模型超过了任何公开可用的单一前沿模型。

结果很直接：**MoA 比 Opus 4.8 强 8%，比 GPT-5.5 强 11%。**

![HermesBench 基准测试对比](https://mmbiz.qpic.cn/mmbiz_jpg/6lygMduFLGQvNzlWDbSmxSJgNnGyu00Etu0nibZ06EUggFqcDmGebficPjL3yt1A17GqJWEARtoibj2q7xrb8If7dSuMGNoRLz6epoRXNIQvjw/640?from=appmsg&watermark=1&tp=webp&wxfrom=5&wx_lazy=1#imgIndex=0)

### 三个臭皮匠，真能顶个诸葛亮

MoA 的思路不复杂，但很聪明。

平时我们讨论 AI 模型，习惯比谁家强、谁家弱。但 Nous 换了个玩法：**不选最强，用组合。**

一次 MoA 调用长这样：

```
用户问题
  ↓
模型 A 先分析（参考模型）
模型 B 先分析（参考模型）
模型 C 先分析（参考模型）
  ↓
聚合模型 读取所有参考意见
  ↓
聚合模型 生成最终回复
  ↓
Hermes Agent 正常执行工具、保存上下文
```

参考模型只读对话文本，不做工具调用——轻量、快速、省成本。聚合模型拿到所有参考意见后，带着完整上下文做最终决策。

这是 **"委员会制" 而不是 "独裁制"**。不是让一个模型拍脑袋，而是多个模型先 "讨论"，再由一个最强的做决定。

Teknium（Nous 的 CEO）说：**你可以把任何供应商的任何模型组合成一个属于自己的混合体，像选普通模型一样直接调用。**

![Hermes Agent MoA 演示动画](data:image/svg+xml,%3C%3Fxml version='1.0' encoding='UTF-8'%3F%3E%3Csvg width='1px' height='1px' viewBox='0 0 1 1' version='1.1' xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'%3E%3Ctitle%3E%3C/title%3E%3Cg stroke='none' stroke-width='1' fill='none' fill-rule='evenodd' fill-opacity='0'%3E%3Cg transform='translate(-249.000000, -126.000000)' fill='%23FFFFFF'%3E%3Crect x='249' y='126' width='1' height='1'%3E%3C/rect%3E%3C/g%3E%3C/g%3E%3C/svg%3E)

### 数据说话：MoA 到底强多少

官方在 HermesBench 上跑了对比：

| 配置 | HermesBench 分数 |
| --- | --- |
| **Opus 聚合 + GPT-5.5 参考（MoA）** | **0.8202** |
| Claude Opus 4.8（单独） | 0.7607 |
| GPT-5.5（单独） | 0.7412 |

MoA 配置比它最强的组成部分（Opus 4.8）高出约 6 分，确认了聚合第二视角带来的质量提升不是平均效应，**是在困难任务上的真实增益。**

用 Nous 自己的话说：这 8%-11% 的提升，意味着 MoA 的产出超过了那些被严格限制访问的顶级模型本身。

### 配置？一句 YAML 就够了

MoA 预设通过正常的模型选择界面就能切换：

```
/model default --provider moa
```

或者用 `/moa` 斜杠命令一键调用：

```
/moa 设计一个微服务迁移方案
```

用完自动恢复之前的模型，不改变对话上下文。

需要自定义的话，写在 YAML 里：

```
moa:
  default_preset: default
  presets:
    default:
      reference_models:
        - provider: openai-codex
          model: gpt-5.5
        - provider: openrouter
          model: deepseek/deepseek-v4-pro
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
      reference_temperature: 0.6
      aggregator_temperature: 0.4
```

就这么简单。**自由组合各大供应商的模型**，OpenAI、Anthropic、DeepSeek 混着用。

每个预设还能单独控制参考模型和聚合模型的 temperature，甚至可以关掉某个预设的参考扩散（`enabled: false`），让聚合模型单独工作。

### 💰 成本惊喜：5 个模型 vs 1 个的价格

你肯定会问：一次调用跑 4 个参考模型加 1 个聚合模型，钱不得烧光？

实际测试结果很有意思。

witcheer 在一台新的 Hermes 实例上实测了成本：用 GPT-5.5、DeepSeek V4 Pro、Sonnet 4.6 做参考，Opus 4.8 做聚合。

| 配置 | Token | 费用 |
| --- | --- | --- |
| 单次 Opus 4.8 调用 | 27.9k | ~$0.14 |
| **完整 MoA 调用（5 模型）** | **28.6k** | **~$0.15** |

差距不到 1 美分。

原因很简单：**系统提示词和工具 Schema 占了大头，而参考模型跑的是剥离上下文**——没有系统提示、没有工具 Schema、只有对话文本。所以 4 个额外调用加起来的 token，还没主模型的零头多。

换句话说，**你花一个模型的钱，请来了一个专家委员会。**

![witcheer 实测 MoA 成本对比](data:image/svg+xml,%3C%3Fxml version='1.0' encoding='UTF-8'%3F%3E%3Csvg width='1px' height='1px' viewBox='0 0 1 1' version='1.1' xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'%3E%3Ctitle%3E%3C/title%3E%3Cg stroke='none' stroke-width='1' fill='none' fill-rule='evenodd' fill-opacity='0'%3E%3Cg transform='translate(-249.000000, -126.000000)' fill='%23FFFFFF'%3E%3Crect x='249' y='126' width='1' height='1'%3E%3C/rect%3E%3C/g%3E%3C/g%3E%3C/svg%3E)

### ⚡ 更聪明的是：Prompt Cache 没碎

MoA 的另一个设计细节容易被忽略，但恰恰是它保持低成本的关键。

聚合模型的上下文里，参考模型的输出被追加到**最新一条用户消息的末尾**——也就是缓存前缀的尾部。这意味着：

-   对话历史、系统提示词、工具 Schema 的缓存前缀**完整保留**
-   只有新追加的参考意见是"新鲜 token"
-   和其他普通对话中新增一条用户消息的开销一样

所以 MoA 的真实成本就是多了几轮轻量参考调用，**而不是缓存被打破后的重新计算。**

### 🤔 那模型怎么搭配？

有人问了一个很实际的问题：**强模型当参考还是当聚合？便宜的模型放哪边？**

官方预设给了一个方向——参考模型用 GPT-5.5 + DeepSeek V4 Pro，聚合模型用 Opus 4.8。但 witcheer 的实测配置也很有参考价值——他在参考层多加了一个 Sonnet 4.6，三个参考模型各给一层视角，成本几乎没变。

建议自己试试不同组合，没有标准答案。MoA 的魅力就在于 **你可以自由实验，找到最适合自己场景的搭配。**

### 为什么这很重要

这个发布的意义不只是 "又多了一个工具"。

**第一，它打破了单模型崇拜。** 过去我们比谁家模型强，选最牛的那个。MoA 的证明是：组合强于单挑。哪怕你拿不到顶级模型的完全访问权，通过组合你能超越它们。

**第二，它在门禁上开了个后门。** 最强模型越来越被公司保护起来（内部使用、API 限流、高定价门槛），但 MoA 的思路是：我不需要打开那扇门，用几个普通模型组合出更好的结果。

**第三，它让模型变成乐高。** 不再是非此即彼的选择。GPT-5.5、Opus、DeepSeek、Sonnet、Mistral——混在一起用，各取所长。

写到这我突然想问你一句：**你现在开发时一般用哪个模型？有没有试过把多个模型组合起来用？**

如果觉得这篇文章有帮助，欢迎点赞、在看、转发！有问题也可以在评论区留言，我会尽量回复！