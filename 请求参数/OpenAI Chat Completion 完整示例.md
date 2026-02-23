普通对话请求

```json
{
  "model": "gpt-4o",
  "messages": [
    { "role": "system", "content": "你是一个helpful assistant" },
    { "role": "user", "content": "你好" }
  ],
  "temperature": 0.7,
  "max_tokens": 1024,
  "stream": false
}
```

返回：

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "gpt-4o-2024-08-06",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好！有什么我可以帮助你的吗？"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 15,
    "total_tokens": 35
  }
}
```

Function Call 请求

```json
{
  "model": "gpt-4o",
  "messages": [{ "role": "user", "content": "北京今天天气怎么样？" }],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "获取城市天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": { "type": "string", "description": "城市名称" }
          },
          "required": ["city"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

返回（模型决定调用工具）：

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_xyz123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"city\": \"北京\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ],
  "usage": {
    "prompt_tokens": 60,
    "completion_tokens": 20,
    "total_tokens": 80
  }
}
```

把工具结果传回去：

```json
{
  "model": "gpt-4o",
  "messages": [
    { "role": "user", "content": "北京今天天气怎么样？" },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_xyz123",
          "type": "function",
          "function": {
            "name": "get_weather",
            "arguments": "{\"city\": \"北京\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_xyz123",
      "content": "北京今天晴，气温 5°C ~ 12°C"
    }
  ]
}
```

最终返回：

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "北京今天天气晴朗，气温在 5°C 到 12°C 之间。"
      },
      "finish_reason": "stop"
    }
  ]
}
```

---

### 流式输出（stream: true）

请求加上 `"stream": true`，返回的是一系列 SSE 事件：

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant","content":""},"index":0}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"content":"你好"},"index":0}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"content":"！"},"index":0}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop","index":0}]}

data: [DONE]
每个 chunk 里 delta.content 是增量文本，最后一条是 [DONE] 表示结束
```
