Mini Coding Agent

Git 仓库地址
https://github.com/Kiki3459/my-coding-agent

一、如何运行
1、安装
进入项目目录：
cd mini-coding-agent

创建虚拟环境：
python3 -m venv .venv
source .venv/bin/activate

安装项目：
python -m pip install -e .

2、模型配置
推荐复制示例文件：
cp .env.example .env

填写：
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=支持工具调用的模型名
OPENAI_BASE_URL=OpenAI兼容接口地址

3、网页使用方式（推荐使用方式）

已安装项目的用户，在项目根目录运行：
source .venv/bin/activate
python -m mini_agent.web_server --open

macOS 也可双击项目中的 `启动网页版.command`。脚本只启动本机服务并打开浏览器，不安装依赖、不自动运行 Agent 任务。

浏览器访问 `http://127.0.0.1:8765`。默认工作区为当前项目，可在网页点击“切换”并输入目标文件夹，例如 `/Users/你的用户名/Desktop`。

网页版提供：

- 多行任务输入；
- 工作区切换、文件夹浏览与只读文件预览；
- 最近任务、执行时间、循环次数和工具调用统计；
- 实时任务记录、可展开工具输出、原始日志和导出；
- 修改差异预览、完整命令预览与独立审批按钮；
- 逐次审批和只读模式、最大循环次数和日志设置；
- 取消任务及正在运行的 Shell 子进程；
- 凭据文件隐藏、本机来源检查、会话令牌校验和输出密钥脱敏。

二、特色功能
本项目不使用任何 Agent 框架或 MCP SDK，基于模型原生 Tool Calling 自行实现完整的编程智能体循环。
Agent 能自主浏览与搜索代码、读取和精确修改文件、执行命令、运行测试，并根据真实报错继续修复。项目支持流式输出和工具参数聚合、结构化错误返回、最大迭代与重复调用检测、Shell 超时及取消机制。
所有文件操作均限制在工作区内，并提供只读模式和逐次审批模式。
系统还实现三级上下文压缩、JSONL 会话持久化、会话恢复、回退与分支、任务计划、Git diff、运行时纠偏及后续任务队列。