"""教师助手的多智能体。

主助手（conductor + router）只做分拣。
两个专家各自带提示词、技能和工具：

- lesson：教案设计与出题专家
- office：教师办公助手

执行循环在 runtime，工具在 tools，名单在 registry。
"""
