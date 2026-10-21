# 本仓库说明（合并版 fork）

本仓库基于 [`aprpure/emby-keeper`](https://github.com/aprpure/emby-keeper)（上游 `emby-keeper/emby-keeper@facccab`，v7.6.2，+43 提交）为基底，
再融合 [`supermoon0201/emby-keeper`](https://github.com/supermoon0201/emby-keeper) 的 Emby 保活改造、
[`Elykia093/emby-keeper`](https://github.com/Elykia093/emby-keeper) 的网页签到器、
以及 [`ignimutos/emby-keeper`](https://github.com/ignimutos/emby-keeper) 的若干真 bug 修复。

版本号：**7.7.0**（基底 7.6.2 + 合并改动）。

---

## 一、需求与实现对照

| 需求 | 实现方式 | 位置 |
|---|---|---|
| 跳过 checkiner 总认证 | `skip_auth = true` 时 `Link.auth("checkiner")` 走 `LocalLink`，直接放行 | `telegram/link.py`、`telegram/link_local.py` |
| 跳过 monitor 总认证 | 同上，`service="monitor"` 也在无条件放行名单内 | 同上 |
| 跳过签到站点 additional_auth | `[llm].skip_additional_auth = true` 时 `LocalLink.supports_service()` 对所有服务返回 True | `telegram/link_local.py` |
| 跳过监控站点 additional_auth | 同上（`monitor/_base.py` 与 `checkiner/_base.py` 都走同一个 `Link.auth()`） | 同上 |
| 避免 auth bot 失效导致流程中断 | 总认证放行 + 本地能力映射 + 失败仅 `IGNORE` 单站点，不中断整体流程 | `link_local.py` |
| `Link.gpt()` 不再走原作者 `/gpt` | 本地 LLM 接管，支持多后端与远端回退开关 | `llm.py`、`link_local.py` |
| 自建 / 反代 / CDN 畸形 Emby | 采用 supermoon0201 的 `emby/api.py`（流读取、错误码、CDN 路径、租约恢复、全直连 DeviceProfile） | `emby/api.py` |
| 网页签到（結束バンド） | 移植 Elykia093 的 `plusemby.py` | `telegram/checkiner/plusemby.py` |

---

## 二、相对 aprpure 基底的改动

### 1. 新增 `[llm].skip_additional_auth`

aprpure 原本是「按能力声明」：配了 LLM 就放行 `prime`/`super`/`pornemby_pack`，
配了 helper 才放行 `captcha`，其余服务返回 False，站点被标记为 `IGNORE`（不中断，但跳过该站点）。

现在增加一个开关，设为 `true` 时**所有** `additional_auth` 一律放行：

```toml
[llm]
skip_additional_auth = true
```

> ⚠️ 这只跳过「认证前置检查」这一关。`captcha` 类站点（`charon`、`future`、`ruji`、`rujing`）
> 后续真正需要验证码令牌时仍会调用 `helper_command`，没配 helper 就会在运行中失败，
> 而不是在初始化时被忽略。想要彻底可用，仍需配置 helper 或保留远端认证。

### 2. Emby 播放侧整体切换到 supermoon0201 的实现

`embykeeper/emby/api.py` 从 1057 行替换为 1406 行的 supermoon 版本，带来：

- 流读取由 `curl_cffi` 改为 `httpx`，手动处理 30x 跨域重定向，单请求 8MB / 15s 分块限流后重连
- 瞬时错误码扩展到 520–525；流错误连续 3 次才抛，且不再中断保活
- `PositionTicks` 夹到 int4 上限 `2147483647`（修部分站点 PG 报 500）
- 结束事件改用 `/Sessions/Playing/Stopped`，`timeupdate` → `TimeUpdate`
- 播放租约失效（`playback_lease_inactive`）自动重发 `/Sessions/Playing` 后重试进度
- 进度上报连续失败 4 次默认抛错，`progress_fallback = true` 可降级为只发停止事件
- `DeviceProfile` 改为全直连（码率 40M → 200M）
- base path 自动探测 `/emby`、`/mediabrowser`
- 取片链路全 `try/except` 降级，Series → Episode 下钻
- 播放成功判定改为「播放前后 UserData 比对」（PlayCount / Played / LastPlayedDate / PlaybackPositionTicks 任一变化），
  未检测到变化时调用 `PlayedItems` 补记，避免站点延迟刷新导致的误判
- 代理凭据 URL 编码（`@`、`:`、`$` 等保留字符不再破坏代理串）

新增账号级配置：`stream_workaround`、`external_stream_workaround`、`stream_cdn_path`、
`progress_fallback`、`timeout`；全局新增 `random_delay`、`random_delay_range`、`timeout`。

同时保留 aprpure 的两个语义改进：

- `emby.account.time` 配置为负数时表示「仅登录，不执行播放保活」
- 单次播放时长裁剪到视频实际时长（`PositionTicks` 不超过 `RunTimeTicks`）

### 3. 移植 Elykia093 的网页签到器（結束バンド / plusemby）

`telegram/checkiner/plusemby.py`（359 行）：

- 走站点 REST API（`/api/auth/login` → `/api/checkin/status` → `/api/checkin`）
- JWT 缓存（`plusemby.access_token.<sha256>`），401 时清缓存重登
- 签到前先查 `hasCheckedInToday`，已签到直接 `NONEED`
- 遇到 Turnstile 时按 `siteKey` 实时求解（token 一次性，写死配置无意义）
- 错误分类：401 登录失效 / 403 安全验证拦截 / 其它取 `message` 字段

依赖 `turnstile.TurnstileService`，本仓库为其补了 `solve_turnstile_async()`（线程池包装同步实现，
不阻塞事件循环），并让它走 aprpure 的多 solver 轮换逻辑，而不是 Elykia 的单 YesCaptcha。

**默认不启用**（模块带 `__ignore__ = True`），需要显式追加：

```toml
[site]
checkiner = ["+plusemby"]

[checkiner."plusemby"]
url = "https://站点地址"
username = "账号"
password = "密码"
# 可选：直接给访问令牌，跳过登录
# access_token = "..."
# 可选：走全局代理
# use_proxy = true
# 可选：临时排障用的静态 Turnstile token（会过期，仅调试用）
# login_verification_token = "..."
# checkin_verification_token = "..."
```

> `+plusemby` 的语义是「在默认站点列表基础上追加」，不是「只启用它」。
> 若只想跑它，用 `checkiner = ["-all", "+plusemby"]`（`-all` 会先清空默认列表）。

### 4. 采用 ignimutos 的真 bug 修复

| 问题 | 原实现 | 修复 |
|---|---|---|
| `interval_days` 区间配置完全失效 | `days[0] + (days[1] - days[0])` 恒等于 `days[1]`，`"<7,12>"` 永远取 12 天 | `random.randint(low, high)` |
| pydantic v2 兼容性 | `__get_validators__`（v1 写法，v3 将移除） | `__get_pydantic_core_schema__` |

### 5. 其它配套

- `cli.py`：`streams` 预置 `None`，修 `-o`/`--once` 单次运行时的 `UnboundLocalError`；
  退出时改用 `_stop_notifier()` 统一 flush 并移除 handler
- `config.py` / `config.example.toml`：新增 `[llm]` 段与 Emby 新选项的注释模板
- 合入 supermoon 的 `tests/test_emby_api.py`（1145 行 / 36 个用例，全部通过）

---

## 三、未采纳的部分

| 来源 | 内容 | 原因 |
|---|---|---|
| ignimutos | `emby/api.py` 拆分为 `transport`/`playback`/`keepalive`/`notification`/`errors` | 与 supermoon 的 `api.py` 重写直接冲突，且该 fork 落后上游 7 个提交（缺 Web 面板） |
| ignimutos `lite` 分支 | `emby.verify`（SSL 校验开关）、凭据 scrypt+Fernet 加密缓存、`speed_limit` 限速 | 这些在 lite 分支（已删除 TG 全家），移植需额外适配；对自签证书站点有价值，可后续单独加 |
| Elykia093 | `_templ_ai.py` 的 AI 视觉接管 | aprpure 的 `llm.visual()` 已覆盖同类能力 |
| Elykia093 | 单 YesCaptcha 的 `turnstile.py` | aprpure 的版本支持多 solver 轮换 + YesCaptcha，更强 |
| Elykia093 | 无条件硬跳过认证（无开关） | 无法回退，且 captcha 类站点会从「初始化拒绝」变成「运行中失败」，更难排查 |
| aprpure | 删除 `ddddocr`，OCR 全部改 LLM | 保留基底现状（见下方「已知限制」） |

---

## 四、已知限制

1. **LLM 是验证码硬依赖**：aprpure 删除了 `ddddocr`，图片验证码路径为
   `_base.py on_photo → OCRService.run() → llm.ocr()`，**没有远端回退**。
   未配置 `[llm].api_key` 时，所有发图片验证码的站点都会失败。
   `vision_model` 必须是真正支持图片输入的模型。

2. **部分站点硬编码了上游打码模型名**：`misty`（默认启用，`digit5-large@v1`）、
   `ljyy` / `ljyy_old`（`uchars4@v1`）、`charon_old`（`words6@v1`）、`jms_old`（`idioms@v2`）
   会把模型名原样传给 OpenAI 兼容接口导致 404。默认配置下需排除 `misty`：

   ```toml
   [site]
   checkiner = ["-misty"]
   monitor = ["-misty"]
   ```

3. **aprpure 遗留的测试失败**：`tests/test_embyboss.py`（2 个）与
   `tests/test_registrar_main.py`（7 个）在基底分支上就是失败的（Fake 对象与实现不同步），
   与本次合并无关。CI 不运行 pytest，不影响构建。

4. **`stream_cdn_path` 是单站特化**：硬编码 `emby-cdn1.` 前缀并伪造客户端头，仅对特定站点有效。

---

## 五、与上游同步

```bash
git remote add upstream https://github.com/emby-keeper/emby-keeper.git
git fetch upstream main
git merge upstream/main     # 冲突集中在 emby/api.py 与 telegram/link*.py
```

基底 aprpure 的改动面很广（83 文件），后续与上游合并会有较多冲突，属于预期成本。
