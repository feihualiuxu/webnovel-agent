# 本地网文写作 Agent 工作流

> 本仓库只包含通用 Agent 引擎、通用配置与使用说明。

这是一个本地文件工作流，用来把一批同题材/同类型小说整理成“可融合的结构素材库”，再生成融合设定、融合大纲、章节细纲、分批正文、审稿归档和整本导出。

它现在包含两种运行方式：

- `autopilot`：自动持续跑，调用模型 API，自己读写项目文件，只在关键节点停下来让你确认/微调。
- `make-prompt`：备用人工模式，把任务交给 Manus / ChatGPT / Claude 网页执行。

它默认不自动登录 ChatGPT / Claude 网页，也不模拟浏览器操作账号。你现有的 GPT Plus、Claude Pro、Manus Pro 如果没有 API key，更适合人工模式；要让本地 agent 自己一直往下跑，需要 OpenAI API、Anthropic API，或 OpenAI-compatible 本地/代理模型服务。

- Manus Pro：做源书拆解、设定包、长上下文项目文件维护。
- GPT Plus / Claude Pro：做 5-10 章一批的正文生成、润色、审稿。
- 本地 CLI：负责导入源书、生成提示词、沉淀长期记忆、做相似度审查、合并导出。

说明：GPT Plus / Claude Pro / Manus Pro 订阅不等于 API key。

## GitHub 工具

我选用 LangGraph 作为可选自动编排引擎。它在 GitHub README 中明确主打 durable execution、human-in-the-loop 和 memory，正好适合长篇写作这种需要长时间运行、断点恢复、人工节点确认的工作流。

安装本地工具：

```powershell
cd ".\agents\webnovel-agent"
.\install_tools.bat
```

安装后 `run.bat` 会自动优先使用 `.venv`。如果你暂时不想装 LangGraph，也可以在 `autopilot` 里加 `--engine builtin`，使用内置状态机。

## 图形控制台

启动本地操作窗口：

```powershell
cd ".\agents\webnovel-agent"
.\gui.bat
```

它会打开 `http://127.0.0.1:8765`，里面可以：

- 打开 ChatGPT / Claude / Manus 官方登录页。
- 打开或启动本地 Sub2API，并把它作为 OpenAI-compatible 模型入口。
- 创建或选择小说项目。
- 选择飞卢 / 番茄 / 起点 / 纵横风格。
- 启动或继续 `autopilot`。
- 查看当前阶段、下一章、正文批次数、摘要数量。
- 在设定冻结、正文批次、审稿、归档节点点击确认继续。
- 打开项目文件夹直接微调设定和正文。

账号登录只发生在官方网页里，控制台不保存账号密码。若要完全自动持续生成，仍建议使用 API key 或 OpenAI-compatible 服务；订阅网页账号更适合在控制台中作为人工/半自动节点使用。

### 使用 Sub2API

你本地已有 `sub2api-local` 时，可以在控制台里：

1. 点“启动 Sub2API”。
2. 点“Sub2API”打开管理页，默认是 `http://localhost:8080`。
3. 在 Sub2API 里确认账号池、模型映射和 API Key。
4. 回到控制台，模型入口选择 `Sub2API / GPT Plus 聚合`。
5. `Base URL` 填 `http://127.0.0.1:8080`。
6. `API Key` 填 Sub2API 里创建的 key。
7. `模型名` 填 Sub2API 支持的模型名，例如你映射出来的 GPT 模型。
8. 先点“测试模型”，成功后再启动自动写作。

注意：Sub2API 的可用模型名、额度和稳定性取决于你自己的账号池与映射配置。控制台只按 OpenAI-compatible 方式调用 `/v1/chat/completions`。

## 支持平台风格

内置可选：

- `feilu`：飞卢，强爽点、快节奏、强钩子。
- `fanqie`：番茄，大众可读、情绪顺滑、追读稳定。
- `qidian`：起点，长线世界观、升级逻辑、角色成长。
- `zongheng`：纵横，剧情张力、命运感、势力冲突。

配置文件在：

`config/platform_profiles.json`

## 快速开始

在 PowerShell 中进入本目录：

```powershell
cd ".\agents\webnovel-agent"
```

一条命令创建项目、导入源书，并让 agent 自动跑到第一个确认节点：

```powershell
set OPENAI_API_KEY=你的_key

.\run.bat autopilot --name "my-first-novel" --platform feilu --source-dir "D:\path\to\source-novels" --provider openai --model gpt-4.1 --max-chapters 30
```

如果使用 Claude API：

```powershell
set ANTHROPIC_API_KEY=你的_key

.\run.bat autopilot --name "my-first-novel" --platform feilu --source-dir "D:\path\to\source-novels" --provider anthropic --model claude-3-5-sonnet-latest --max-chapters 30
```

如果使用本地或代理的 OpenAI-compatible 服务：

```powershell
.\run.bat autopilot --name "my-first-novel" --platform feilu --source-dir "D:\path\to\source-novels" --provider openai-compatible --base-url "http://127.0.0.1:8000" --model "你的模型名" --max-chapters 30
```

到确认节点后，你可以直接修改项目文件，然后确认继续：

```powershell
.\run.bat approve --project ".\projects\my-first-novel"

.\run.bat autopilot --project ".\projects\my-first-novel" --provider openai --model gpt-4.1 --max-chapters 60
```

查看自动状态：

```powershell
.\run.bat agent-status --project ".\projects\my-first-novel"
```

深读源书模式会逐块分析源书，质量更高但更耗费 API：

```powershell
.\run.bat autopilot --project "D:\...\projects\my-first-novel" --provider openai --source-mode deep --max-source-chunks-per-book 40 --max-chapters 30
```

备用：一条命令创建项目、导入源书，并生成第一步融合拆解提示词：

```powershell
.\run.bat start --name "my-first-novel" --platform feilu --source-dir "D:\path\to\source-novels" --provider manus
```

也可以分步执行。先创建项目：

```powershell
.\run.bat init --name "my-first-novel" --platform feilu --source-dir "D:\path\to\source-novels"
```

导入源书：

```powershell
.\run.bat ingest --project ".\projects\my-first-novel"
```

生成源书拆解提示词：

```powershell
.\run.bat make-prompt --project ".\projects\my-first-novel" --stage decompose --provider manus
```

把 `04_prompts` 中生成的提示词复制到 Manus Project。Manus 输出后，把结果保存到 `10_inbox`，再归档：

```powershell
.\run.bat accept --project ".\projects\my-first-novel" --kind analysis --file ".\projects\my-first-novel\10_inbox\manus_decompose_output.md"
```

生成融合设定包提示词：

```powershell
.\run.bat make-prompt --project ".\projects\my-first-novel" --stage blueprint --provider manus
```

生成正文批次提示词：

```powershell
.\run.bat make-prompt --project ".\projects\my-first-novel" --stage chapters --provider claude --start 1 --count 10
```

正文输出保存到 `10_inbox` 后归档：

```powershell
.\run.bat accept --project ".\projects\my-first-novel" --kind draft --file ".\projects\my-first-novel\10_inbox\ch_0001_to_0010.md" --start 1 --end 10
```

审查与源书长串重合：

```powershell
.\run.bat audit --project ".\projects\my-first-novel" --draft ".\projects\my-first-novel\05_drafts\ch_0001_to_0010.md"
```

导出整本：

```powershell
.\run.bat export --project ".\projects\my-first-novel"
```

## 推荐完整流程

自动模式：

1. `install_tools.bat`：安装 LangGraph。
2. 设置 API key。
3. `autopilot`：自动导入、拆解、生成融合设定包。
4. 设定冻结节点：你修改/确认 `03_new_novel`。
5. `approve` 后继续 `autopilot`。
6. 它会按批次写正文、更新摘要、更新长期记忆。
7. 每批、每 20 章审稿、每 50 章归档都会停下让你确认。
8. 全部完成后 `export`。

人工备用模式：

1. `init`：选平台，建项目。
2. `ingest`：导入源书，生成源书清单和抽样卡片。
3. `make-prompt --stage decompose`：让 Manus 拆解源书。
4. `make-prompt --stage blueprint`：融合成新书设定包、卷纲和前 200 章细纲。
5. `make-prompt --stage chapters --start 1 --count 10`：每次 5-10 章。
6. `accept`：归档输出。

## 借鉴融合口径

这里不是要求从零原创大纲，而是以源书为底座做融合重组：

- 可以吸收：题材、爽点、节奏、冲突模型、人物功能位、金手指逻辑、世界规则思路、剧情模块、高潮模型。
- 必须重组：主线、世界观、角色关系、金手指体系、反派升级链、结局。
- 不要直接照搬：源书原文、专有人名、组织名、完整桥段、标志性台词、章节顺序和可识别情节链。

目标效果是：读者能明显感觉这本书吸收了源书库的卖点、节奏和题材优势，但成书结构统一，不像多本书硬拼。

## 目录结构

每个小说项目会生成：

- `00_config`：项目配置。
- `01_sources`：源书索引、清单、抽样卡片。
- `02_source_analysis`：源书拆解结果和素材池。
- `03_new_novel`：新书 Bible、风格、世界观、金手指、卷纲、章纲、长期记忆。
- `04_prompts`：给 Manus / ChatGPT / Claude 的提示词。
- `05_drafts`：正文批次。
- `06_summaries`：批次摘要。
- `07_reviews`：审稿和相似度审查。
- `08_exports`：整本导出。
- `09_handoff`：长上下文交接包。
- `10_inbox`：临时存放模型输出。

## 查看状态

```powershell
.\run.bat status --project "D:\...\projects\my-first-novel"
```
