"""core —— 核心引擎子包。

承载 AgentKit 运行时的核心组件：
    - agent:      智能体主体，串联 LLM / Tool / Skill
    - context:    会话上下文，承载消息、变量、大对象摘要
    - workflow:   工作流编排，将多个 Step 组合执行
    - hooks:      生命周期钩子，在 Step 执行前后注入副作用
    - checkpoint: 检查点，用于断点续跑与状态持久化
    - template:   模板引擎，{{var}} / ${ENV} 解析与表达式求值
"""