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
cp -R termfix/*.py ~/Library/Application\ Support/iTerm2/Scripts/AutoLaunch/
```

目录结构应为：

```
~/Library/Application Support/iTerm2/Scripts/AutoLaunch/
├── termfix.py        ← 入口文件
├── monitor.py
├── context.py
├── llm_client.py
├── ui.py
└── config.py
```

### 第四步：安装 `openai` 依赖

优先使用 iTerm2 内置的 Python 运行时：

```bash
~/.iterm2_venv/bin/pip install openai
```

如果上述路径不存在，使用系统 Python：

```bash
pip3 install openai
```

### 第五步：启动脚本

**iTerm2 → Scripts → AutoLaunch → termfix.py**

或直接重启 iTerm2——AutoLaunch 目录下的脚本会自动运行。

脚本运行后，在 iTerm2 底部状态栏可以看到 `✅` 图标。

### 第六步：添加到状态栏

1. **iTerm2 → Preferences → Profiles → 选择你的 Profile → Session**
2. 点击底部 **Configure Status Bar**
3. 将 **TermFix** 从组件列表拖入激活区域
4. 点击 **OK**

### 第七步：配置兼容 API

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

安装完成后无需手动操作。

1. 在终端运行任意失败命令，例如：
   ```bash
   git psuh   # 拼写错误
   npm run buid  # 目标不存在
   python script.py  # 脚本有 bug
   ```

2. 状态栏从 `✅` 变为 `🔴 Fix (1)`

3. 点击图标，稍等片刻（首次点击调用兼容 API），弹窗展示分析结果

4. 弹窗关闭后错误计数清零，状态栏恢复 `✅`

## 项目结构

```
termfix/
├── termfix.py      主入口，iterm2.run_forever(main)
├── monitor.py      TermFixState 共享状态；全局 PromptMonitor 路由器；
│                   per-session asyncio worker task
├── context.py      从 iTerm2 session 收集终端输出、CWD、shell 类型
├── llm_client.py   OpenAI 兼容接口调用（流式）
├── ui.py           StatusBarComponent 注册、onclick 处理、HTML 弹窗
└── config.py       常量、默认值、系统 prompt
```

## 故障排查

**状态栏没有出现 TermFix 图标**

- 确认脚本已启动：**iTerm2 → Scripts → AutoLaunch**，检查 `termfix/termfix.py` 旁边是否有运行指示
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

**`pip install openai` 找不到 `~/.iterm2_venv`**

- 在 iTerm2 中执行 **Scripts → Manage Dependencies**，通过内置界面安装 `openai`
- 或在 Script Console 中查看 Python 可执行文件路径，用对应的 pip 安装
