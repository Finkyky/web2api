# 项目业务架构（重构方案）

## 1. 数据模型

数据库中按**代理 IP（指纹 ID）**分组，每个 IP 组下可配置账号。账号字段：

- **名称**：显示用
- **类别（type）**：如 `claude`、`chatgpt`、`kimi`
- **认证字段（auth）**：一个 JSON，具体 key 由各 type 的插件决定（如 claude 用 `sessionKey`，其他可能用 token 等）

约定：**一个账号记录只属于一个 type**。同一代理人在 DB 中可有多条记录（如一条 type=claude，一条 type=chatgpt）。**不设 profile_id**，user-data-dir 按系统现有拼接方式（如按指纹/IP 组等）生成。

## 2. 缓存层次（树形结构）

- **浏览器**：缓存的是**进程（process）**，对应唯一指纹 ID（一个 IP 组一个进程）
- **context**：CDP 连接，挂在浏览器下
- **page**：CDP 连接，挂在 context 下；同一 context 下**每个 type 一个 page**（一个 tab），按 type 复用；若某 type 需要多步 URL（如登录再对话），在同一 page 内完成，不开多个 page
- **会话**：挂在 page 下；**一个 page 下可有多个会话**。会话由「会话 ID」绑定，**会话 ID 全局唯一**，匹配到即可向上查找到对应 page、context。进程内缓存，不跨进程、不持久化。

结构示意：

```
浏览器（对应唯一指纹，缓存 process）
  └── context（CDP 连接）
        └── page（CDP 连接，一个 type 一个 page）
              └── 会话（多个，由 session_id 区分）
```

## 3. 接口形态

- 协议：OpenAI 兼容格式（如 `/v1/chat/completions`）
- 同一进程、同一端口，用 path 前缀区分 type：
  - `baseUrl = http://ip:port/type`
  - 例：`http://ip:port/claude/v1/chat/completions`、`http://ip:port/kimi/v1/chat/completions`
- 请求 `/type/v1/chat/completions` 时，仅从「类别 = type」的账号中做 acquire

## 4. 会话 ID 的携带方式

**请求中**：在 content 里必须写成 HTML 注释形式：

- 格式：`<!-- conv_uuid=xxx -->`，其中 `xxx` 为会话 ID
- 基础架构从 messages 的 content 中解析该标记，若存在且会话缓存中有对应项则复用（通过 session_id 向上查到 page、context），否则创建新会话

**响应中**（把 session_id 交给客户端以便下次复用）：流式、非流式均在**返回消息的最前面**加一条同格式注释 `<!-- conv_uuid=xxx -->`，xxx 为本次会话 ID（首次创建时由服务端生成并在此返回）

## 5. 请求处理流程

用户调用 `http://ip:port/type/v1/chat/completions` 时：

1. 从 messages 的 content 中解析是否携带 `conv_uuid=xxx`
2. 若有且会话缓存中存在该会话 ID → 复用该会话
3. 若无或缓存中无 → 需要创建新会话，再按下列分支执行：

```
if (存在浏览器 context) {
    if (存在该 type 对应的 page) {
        if (message 中携带 conv_uuid 且 会话缓存 存在此 ID) {
            复用该会话，发送请求
        } else {
            创建新会话，发送请求
        }
    } else {
        创建新 page、登录（插件实现）、创建会话、发送请求
    }
} else {
    打开浏览器、打开该 type 的 page（插件提供）、CDP 连接、登录（插件实现）、创建会话、发送请求
}
```

## 6. 插件式架构

支持某种 type（如 claude）= 引入对应插件并注册。基础架构负责：

- 按 (指纹 ID, type) 管理 context / page / 会话缓存
- 解析 content 中的 `conv_uuid=xxx`
- 按 path 的 type 路由到对应插件
- 从 DB 加载账号时按 type 过滤后 acquire

各 type 的「如何打开 page、如何登录、如何创建/复用会话、如何发 completion」由插件实现。**429 与账号封禁、重试策略**也由插件实现（不同网站策略不同），基础架构不统一处理。

## 7. 插件接口草案（由现有 web2api 反推）

以下为最小可用的插件接口约定，便于实现「基础架构 + 第一个 claude 插件」。

| 能力           | 接口约定                                                                                          | 说明                                                                                                                            |
| -------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 打开/复用 page | `ensure_page(context: BrowserContext) -> Page`                                                    | 若 context 中已有该 type 的 page（如 URL 匹配）则复用，否则 `context.new_page()` 并 goto 目标 URL                               |
| 应用认证       | `apply_auth(context: BrowserContext, page: Page, auth: dict) -> None`                             | 用账号的 auth JSON 写 cookie / 设 header 等，必要时 `page.reload()`                                                             |
| 创建会话       | `create_conversation(context: BrowserContext, page: Page) -> str \| None`                         | 调用该 type 的 API 创建会话，返回会话 ID（如 conversation_uuid）；失败返回 None                                                 |
| 流式补全       | `stream_completion(context, page, session_id: str, message: str, **kwargs) -> AsyncIterator[str]` | 在已有会话上发一条 message，逐块 yield 助手回复；会话续写所需的额外状态（如 parent_message_uuid）由插件在内部或通过 kwargs 维护 |

可选扩展（按需由插件实现）：

- **解析 content 中的会话 ID**：若某 type 的会话 ID 格式与统一的 `conv_uuid=xxx` 不同，可由插件提供 `parse_session_id(messages) -> str | None`，基础架构优先用插件解析结果再回退到默认 `conv_uuid=xxx`。
- **会话元数据**：若插件需要保存与会话绑定的额外数据（如 Claude 的 org_uuid、parent_message_uuid），由插件在内存中按 session_id 维护，或由基础架构提供 `SessionState` 字典由插件读写。

注册方式建议：插件在加载时向全局 Registry 注册 `type_name -> 上述接口实现`，基础架构根据 path 中的 type 查找并调用。
