# Function Call 层设计文档

## 1. 概述

### 1.1 目标

在 Claude 网页版（仅支持纯文本）与 Cursor（OpenAI 工具调用接口）之间，增加一层协议转换，使 Cursor 能通过本服务使用 Claude 的 tool calling 能力。

### 1.2 设计思路

- **底层**：模型只输出纯文本
- **约定**：Claude 按 `<tool_call>{"name":"xxx","arguments":{...}}</tool_call>` 格式输出
- **本层职责**：解析该格式，转换为 OpenAI `tool_calls` 结构；将 tools 拼入 prompt，将 tool 结果拼回 prompt 再发给 Claude

---

## 2. 数据流

```
Cursor (OpenAI 格式)  ←→  Function Call 层  ←→  Claude 网页版 (纯文本)
  tools, tool_calls        解析 / 转换           <tool_call> 文本
```

---

## 3. 模块

| 文件                        | 职责                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------- |
| `core/api/function_call.py` | parse_tool_calls、format_tools_for_prompt、detect_tool_call_mode、build_tool_calls_response |
| `core/api/schemas.py`       | OpenAIMessage（tool_calls, tool_call_id）、extract_user_content                             |
| `core/api/chat_handler.py`  | 将 tools 转文本，拼入 prompt 后发给插件                                                     |
| `core/api/routes.py`        | 流式/非流式中解析 `<tool_call>`，转成 OpenAI 响应                                           |

---

## 4. 请求处理（Cursor → Claude）

- `extract_user_content(messages, tools_text)`：将 user、assistant（含 tool_calls）、tool 消息拼成 prompt
- `format_tools_for_prompt(tools)`：把 Cursor 的 tools 转为易读文本
- tools 说明 + 对话内容 + `请继续完成任务。` 组成完整 prompt

---

## 5. 响应处理（Claude → Cursor）

- `parse_tool_calls(text)`：提取所有 `<tool_call>...</tool_call>` 块
- `detect_tool_call_mode(buffer)`：流式中提前判断是否为 tool_call 模式
- `build_tool_calls_response` / `build_tool_calls_chunk`：转为 OpenAI 格式

---

## 6. 流式策略

- 边收边判断：`strip_session_id` 后若以 `<tool_call>` 开头 → 缓冲模式
- 若内容已超过 11 字符且未以 `<tool_call>` 开头 → 普通文本，流式输出
- 缓冲结束后解析，若有 tool_calls 则一次性发出 tool_calls chunk

---

## 7. 格式约定

Claude 输出示例：

```
<tool_call>{"name":"Read","arguments":{"path":"/path/to/file"}}</tool_call>
```

多个 tool_call：

```
<tool_call>{"name":"Read","arguments":{"path":"a.py"}}</tool_call>
<tool_call>{"name":"Grep","arguments":{"pattern":"def","path":"a.py"}}</tool_call>
```
