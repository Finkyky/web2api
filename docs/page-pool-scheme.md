# Page 池方案：每 type 多 page，用后关闭并补回

## 目标

- 每个 (proxy_key, type) 维护一个 **page 池**，池大小可配置（默认 3）。
- 请求来时从池里**取走一个 page**，用完后**关闭该 page**，并**异步再开一个 page 放回池**，保证池内始终有可用 page（或正在补回）。
- 避免同一 page 上多路 evaluate 并发，同时避免每次请求都临时 `new_page()` 的固定延迟（池内已有 page 可直接用）。

## 行为约定

1. **池创建时机**：首次有请求需要 (proxy_key, type) 时创建池（lazy），并预先创建 N 个 page 填满池。
2. **取 page**：从池中 get（若池空则等待，直到有 page 被还回或补回）。
3. **还 page**：请求结束时**不**把 page 还回池，而是 **close 该 page**，并 **asyncio.create_task 补一个新 page 入池**，不阻塞当前请求返回。
4. **池大小**：目标容量 N（默认 3），由配置或 BrowserManager 参数决定；实际瞬时可用数可能为 0 ～ N（有请求占用时减少，补回后恢复）。

---

## 需要改动的模块

### 1. 插件接口：`core/plugin/base.py`

- **新增**：`create_page(self, context: BrowserContext) -> Coroutine[Any, Any, Page]`

  - 语义：**总是**新建一个 page 并打开该 type 的入口 URL（不复用已有 page）。
  - 用于：池子初始化、用后补回时创建新 page。
  - 默认实现：`raise NotImplementedError`；各插件必须实现。

- **保留**：`ensure_page(context)` 可保留用于其他场景（若有），或标记为 deprecated；本方案以池 + `create_page` 为主。

## 批注:无需保留

### 2. 通用 helpers：`core/plugin/helpers.py`

- **新增**：`create_page_for_site(context, start_url, *, timeout=20000) -> Page`
  - 实现：`page = await context.new_page()`，`await page.goto(start_url, ...)`，`return page`。
  - 供各插件在 `create_page(context)` 里复用（如 Claude：`return await create_page_for_site(context, CLAUDE_START_URL)`）。

---

### 3. Claude 插件：`core/plugin/claude.py`

- **实现**：`async def create_page(self, context: BrowserContext) -> Page`
  - 内部调用：`return await create_page_for_site(context, CLAUDE_START_URL)`。
- 若不再需要「单页复用」逻辑，可不再实现或简化 `ensure_page`（若 base 仍要求实现，可让 `ensure_page` 内部调用一次 `create_page` 或保留现有 `ensure_page_for_site` 单页逻辑，由你决定是否统一成只走池）。

批注：统一走池

---

### 4. 浏览器管理器：`core/runtime/browser_manager.py`

- **配置**

  - 增加构造参数或配置项：`page_pool_size: int = 3`（可配置，默认 3）。

- **数据结构**

  - 将 `_BrowserEntry.pages: dict[str, Page]` 改为 **按 type 的 page 池**：
    - 例如：`page_pools: dict[str, PagePool]`，其中 `PagePool` 至少包含：
      - `queue: asyncio.Queue[Page]` 存当前可用 page；
      - `create_page_fn: EnsurePageFn`（或新类型，如 `CreatePageFn`）；
      - `context: BrowserContext`（用于补 page 时调用 `create_page_fn(context)`）；
      - 可选：`target_size: int`（用于补回时尽量维持池大小）。

- **创建池**

  - 在「首次为该 (proxy_key, type) 要 page」时：
    - 若该 type 尚无 pool，则新建 `PagePool`，并循环 `page_pool_size` 次：`page = await create_page_fn(context)`，`queue.put_nowait(page)`。
    - 同时维护 refcount（例如：有一个 pool 即 refcount += 1，与现有「每 type 占一 ref」语义一致，便于 release 时清空该 type 的 pool 并关浏览器）。

- **取 page**

  - 新 API：`async def get_page_from_pool(proxy_key, context, type_name, create_page_fn) -> Page`
    - 若该 type 无 pool，先按上一步创建并填满池。
    - `return await pool.queue.get()`（必要时带超时，避免永久阻塞）。

- **还 page（用后关闭并补回）**

  - 新 API：`def release_page(proxy_key, type_name, page) -> None`（或 `return_page`）：
    - `await page.close()`（或 schedule 关闭，不阻塞调用方）；
    - `asyncio.create_task(_replenish_page(proxy_key, type_name))`：
      - `_replenish_page` 内：`new_page = await create_page_fn(entry.context)`，`entry.page_pools[type_name].queue.put_nowait(new_page)`；
      - 注意异常与日志，避免补回失败导致池永远少一页。
  - **不**把刚用过的 page 再放回池（始终关闭，避免状态污染）。

- **release(proxy_key, type_name)**（现有「该 type 不再用此浏览器」逻辑）

  - 若存在该 type 的 pool：清空 queue 中所有 page（逐个 close），删除 pool，refcount -= 1，若 refcount <= 0 则关浏览器。
  - 与「单次请求结束」的 `release_page` 区分开：前者是「整个 type 不再用这个浏览器」，后者是「这次请求用完了 page」。

- **兼容**
  - 将原来的 `get_or_create_page(..., ensure_page_fn)` 改为内部调用 `get_page_from_pool(..., create_page_fn)`；若现有调用方传的是 `ensure_page`，需要改为传 `create_page`，或在 manager 内对「只有 ensure_page、没有 create_page」的插件做兼容（例如第一次用 ensure_page 拿一页并当作池大小为 1 的池）。建议统一要求插件提供 `create_page`，池大小至少为 1。

---

### 5. 调用方：`core/api/chat_handler.py`

- **取 page**

  - 将 `get_or_create_page(proxy_key, context, type_name, plugin.ensure_page)` 改为：
    - `get_page_from_pool(proxy_key, context, type_name, plugin.create_page)`（或保留方法名 `get_or_create_page` 但参数改为 `create_page_fn`，见上）。
  - 若插件未实现 `create_page`，可回退到 `ensure_page` 并打日志，或直接报错。

- **用后归还**

  - 在**使用 page 的整段逻辑**外包一层 `try/finally`：
    - 在 `finally` 中调用 `browser_manager.release_page(proxy_key, type_name, page)`（或 `return_page`），确保无论 stream 成功、抛错、AccountFrozenError 重试，只要「这份 page 用完了」就关闭并触发补回。
  - 注意：同一请求若因 AccountFrozenError 重试，会先 release 当前 page，再在下一轮用新取的 page（从池里再 get 一个），逻辑正确。

- **create_conversation / apply_auth**
  - 仍使用当前取到的 page；若 `create_conversation` 或插件内部依赖「同一 context 下已有某页」，现在改为「每次都是池里的一页」，需确认插件能接受（当前 Claude 是 context.request + 任意一页即可，应兼容）。

---

### 6. 配置与常量

- **池大小**
  - 来源可选：
    - 环境变量，如 `PAGE_POOL_SIZE`；
    - 或 `core/config` 里为 runtime 增加一项（如 `page_pool_size`），在 app 组装 BrowserManager 时传入；
    - 或直接写死在 BrowserManager 构造参数，默认 3。
  - 建议先做「BrowserManager 构造参数，默认 3」，后续再接到统一配置。

---

### 7. 其他调用 get_or_create_page 的地方

- 全局搜索 `get_or_create_page` / `ensure_page`，确保：
  - 所有需要「从池取 page」的路径都改为 `get_page_from_pool` + `release_page`；
  - 没有遗漏的「用完后未 release_page」的路径，否则会漏关 page、漏补回，池会被掏空。

---

## 流程小结

1. 请求进入 → 解析 type、proxy_key，`ensure_browser(proxy_key)`。
2. `get_page_from_pool(proxy_key, context, type_name, plugin.create_page)`：
   - 若该 type 无池则建池并预填 N 个 page；
   - 从池中 get 一个 page 返回。
3. 使用该 page：apply_auth、create_conversation、stream_completion 等。
4. 在 `finally` 中调用 `release_page(proxy_key, type_name, page)`：关闭 page，并 `create_task` 补一个新 page 入池。
5. 若同一 type 需要 release 浏览器（切 proxy），则 `release_async(proxy_key, type_name)` 清空该 type 的池并关浏览器（保持现有逻辑）。

---

## 边界情况

- **池空**：并发请求多时，池可能暂时为空，`get_page_from_pool` 会 await queue.get()，直到有 page 被补回或之前占用的请求归还并触发补回。可给 queue.get 加超时（如 60s）避免永久阻塞，超时则报错「暂时无可用 page」。
- **补回失败**：`_replenish_page` 里若 `create_page_fn` 抛错，记录日志并可选地重试一次；池大小会暂时少 1，下次补回成功会恢复。
- **浏览器被关闭**：若在补回过程中该 proxy_key 的浏览器被 release 掉，需在 `_replenish_page` 里判断 entry 是否仍存在、context 是否有效，避免在已关闭的 context 上 new_page。

按上述改完后，即可实现「每 type 默认 3 个 page、用后关闭并异步补回」的池化方案；需要我按文件写出具体 diff 再继续。
