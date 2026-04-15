# TermFix

iTerm2 插件，自动捕获命令失败（exit code ≠ 0），通过 OpenAI 兼容 API 分析错误原因，在状态栏弹窗中展示修复建议。

## 效果

```
正常状态   ✅
有错误时   🔴 Fix (2)   ← 角标显示未处理的错误数量
```

点击 `🔴 Fix (2)` 后，模型分析失败命令并弹出：

- **错误原因** — 简洁的根因说明
- **修复命令** — 可直接执行的 shell 命令
- **详细解释** — 帮助理解问题背景

## 前置条件

| 依赖 | 版本要求 |
|------|----------|
| iTerm2 | ≥ 3.4，需开启 Python API |
| Python | ≥ 3.8（iTerm2 内置运行时或系统 Python） |
| Shell Integration | 需在每个 session 中安装 |
| 兼容 OpenAI 的 API Key | 例如 DeepSeek 控制台生成的 key |

## 安装

### 第一步：开启 iTerm2 Python API

**iTerm2 → Preferences → General → Magic**，勾选 *Enable Python API*。

### 第二步：安装 Shell Integration

**iTerm2 → Install Shell Integration**，按提示在每个 shell profile 中安装。  
重启终端后生效（shell integration 是 PromptMonitor 的数据来源）。

### 第三步：复制插件文件

```bash
./setup.sh
```

目录结构应为：

```
~/Library/Application Support/iTerm2/Scripts/
├── AutoLaunch/
│   └── termfix.py
└── termfixlib/
    ├── __init__.py
    ├── config.py
    ├── context.py
    ├── llm_client.py
    ├── monitor.py
    └── ui.py
```

以后修改代码后，重新执行一次即可同步到 iTerm2：

```bash
./setup.sh
```

如果你的 iTerm2 使用自定义 Scripts 目录，可以显式指定：

```bash
ITERM2_SCRIPTS_DIR="$HOME/.config/iterm2/AppSupport/Scripts" ./setup.sh
```

### 第四步：启动脚本

**iTerm2 → Scripts → AutoLaunch → termfix.py**

或直接重启 iTerm2——AutoLaunch 目录下的脚本会自动运行。

脚本运行后，在 iTerm2 底部状态栏可以看到 `✅` 图标。

### 第五步：添加到状态栏

1. **iTerm2 → Preferences → Profiles → 选择你的 Profile → Session**
2. 点击底部 **Configure Status Bar**
3. 将 **TermFix** 从组件列表拖入激活区域
4. 点击 **OK**

### 第六步：配置兼容 API

点击状态栏中的 TermFix 组件，选择 **Configure**（或在 Status Bar 设置中双击组件），填写以下 knob：

| Knob | 说明 | 默认值 |
|------|------|--------|
| **Base URL** | OpenAI 兼容接口地址 | `https://api.deepseek.com` |
| **API Key** | 提供商 API Key | 空，**必填** |
| **Model** | 使用的模型 | `deepseek-chat` |
| **Context Lines** | 捕获的终端行数 | `50` |

DeepSeek 推荐填写：

| 字段 | 值 |
|------|----|
| `base_url` | `https://api.deepseek.com` |
| `api_key` | `sk-xxxx` |
| `model` | `deepseek-chat` |

如果你想要更强推理，也可以把 `model` 改成 `deepseek-reasoner`。

## 使用方式

安装完成后，TermFix 会记录失败命令，但分析由你手动触发。

1. 在终端运行任意失败命令，例如：
   ```bash
   git psuh   # 拼写错误
   npm run buid  # 目标不存在
   python script.py  # 脚本有 bug
   ```

2. 状态栏从 `✅` 变为 `🔴 Fix (1)`

3. 按 `Cmd+J` 手动触发分析并打开弹窗；也可以点击状态栏里的 `TermFix`

4. 弹窗打开后会流式刷新模型输出，不会为了刷新内容反复重新弹出窗口

5. 弹窗打开时，再按一次 `Cmd+J` 会关闭弹窗

6. 弹窗关闭后结果不会被清空。你可以再次按 `Cmd+J` 或点击 `TermFix` 重新打开同一条结果

### 手动提问

按 `Cmd+L` 会在当前 iTerm2 session 打开一个输入弹窗。输入自己的 prompt 后按 `Enter` 发送，`Shift+Enter` 可换行。模型回复完成后，输入框会重新启用，可以继续追问。

弹窗左侧会显示当前 session 的对话历史。点击历史项可以 resume 之前的对话；点击 **New** 可以开始新对话。

这类请求不会增加错误计数。模型消息结构为：

- **system prompt**：说明当前环境是 iTerm2 命令行，并包含当前 session 最近 50 行命令行内容、工作目录、shell 和系统信息
- **conversation messages**：只包含这个弹窗内的多轮 user / assistant 对话内容；每次新问题仍由用户输入作为最新 user message

## 项目结构

```
termfix/
termfix.py          iTerm2 AutoLaunch 入口
termfixlib/
├── monitor.py      TermFixState 共享状态；全局 PromptMonitor 路由器；
│                   per-session asyncio worker task
├── context.py      从 iTerm2 session 收集终端输出、CWD、shell 类型
├── llm_client.py   OpenAI 兼容接口调用（标准库 HTTP）
├── ui.py           StatusBarComponent 注册、快捷键处理、HTML 弹窗
└── config.py       常量、默认值、系统 prompt
```

## 故障排查

**状态栏没有出现 TermFix 图标**

- 确认脚本已启动：**iTerm2 → Scripts → AutoLaunch**，检查 `termfix.py` 旁边是否有运行指示
- 确认 `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/` 下只有 `termfix.py`，不要把 `config.py`、`ui.py` 这类辅助模块放进 AutoLaunch
- 查看脚本控制台日志：**iTerm2 → Scripts → Open Script Console**
- 确认已在 Status Bar 设置中拖入 TermFix 组件

**命令失败后状态栏没有变化**

- Shell Integration 未安装或未生效，重新执行 **iTerm2 → Install Shell Integration** 并重启终端
- 查看脚本控制台确认没有 Python 异常

**点击图标后弹窗显示"No API key set"**

- 双击状态栏组件进入 knob 配置，填写 API Key

**点击后长时间无响应**

- 检查网络是否能访问你配置的 `Base URL`
- 查看脚本控制台的错误日志
- 确认 API Key 有效

**安装时遇到 `Manage Dependencies` / `pip install openai` 报错**

- 当前版本已经不依赖 `openai` 包，不需要执行这一步
- 确认 `termfix.py` 在 `AutoLaunch/`，而 `termfixlib/` 在 `Scripts/` 根目录
