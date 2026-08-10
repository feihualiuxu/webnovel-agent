# Kimi K3 接入

网文 Agent 已内置 `kimi` provider，默认配置：

- 模型：`kimi-k3`
- Base URL：`https://api.moonshot.cn/v1`
- 协议：OpenAI-compatible Chat Completions
- 默认推理强度：`low`

## 图形控制台

运行 `gui.bat`，在“模型入口”中选择 `Kimi K3 API`，填入 Kimi API Key 后先点击“测试模型”。

## 命令行

PowerShell：

```powershell
$env:KIMI_API_KEY = "你的_key"
.\run.bat autopilot --name "my-first-novel" --platform feilu --source-dir "D:\path\to\source-novels" --provider kimi --model kimi-k3
```

多个 Key 可以写入 `KIMI_API_KEYS`，用换行、逗号或分号分隔；调用失败或额度耗尽时会自动轮换。

可选环境变量：

- `KIMI_API_KEY` / `KIMI_API_KEYS`
- `KIMI_MODEL`，默认 `kimi-k3`
- `KIMI_BASE_URL`，默认 `https://api.moonshot.cn/v1`
- `KIMI_REASONING_EFFORT`，默认 `low`

如果你的 Kimi 账号返回了不同的正式模型 ID，可通过 `--model` 或 `KIMI_MODEL` 覆盖，不需要修改源码。
