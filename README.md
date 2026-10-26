# Embykeeper · cloudsonly fork

[![CI](https://img.shields.io/github/actions/workflow/status/cloudsonly/emby-keeper/ci.yml?branch=main&label=CI)](https://github.com/cloudsonly/emby-keeper/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/ghcr.io-cloudsonly%2Femby--keeper-blue)](https://github.com/cloudsonly/emby-keeper/pkgs/container/emby-keeper)
[![Version](https://img.shields.io/badge/version-7.7.0-green)](https://github.com/cloudsonly/emby-keeper)
[![License](https://img.shields.io/github/license/cloudsonly/emby-keeper)](https://github.com/cloudsonly/emby-keeper/blob/main/LICENSE)

<p align="center">
  <a href='https://github.com/cloudsonly/emby-keeper'>
    <img src="https://github.com/emby-keeper/emby-keeper/raw/main/images/logo.svg" alt="Embykeeper" />
  </a>
</p>
<p align="center">
    <b>自动签到 · 定时保号 · 按需水群</b>
</p>

---

## 这是什么

[emby-keeper/emby-keeper](https://github.com/emby-keeper/emby-keeper) 的合并增强版 fork。保留上游全部功能，另外解决三件事：

| 问题 | 本 fork 的做法 |
|---|---|
| 应对上游的云端认证服务离线 | 验证码解析、考核问答、看图选按钮改用本地 LLM，不向第三方发送内容 |
| 自建 / 反代 / CDN 的 Emby 站点保活失败 | 补齐畸形站点适配：分块限流、错误码 520–525、租约恢复、CDN 取流、进度降级 |
| 网页签到站点缺失 | 补上纯网页签到（不依赖 Telegram 机器人） |

核心功能仍是两个：**TG 机器人签到**（数十个站点每日签到）与 **Emby 保号**（模拟播放保持账号活跃）；此外提供自动抢注、群组游戏、考核辅助。

基于上游 `main` 分支 fast-forward 演进，同步上游只需一次 `git merge`。只发布容器镜像，不发布 PyPI 包。

---

## 快速开始

**首次运行**（交互式，按提示输入手机验证码）：

```bash
docker run -v $(pwd)/app:/app --rm -it --net=host ghcr.io/cloudsonly/emby-keeper:latest -i
```

`-i` 表示启动后立刻执行一次任务；`--net=host` 仅在 Linux 生效，其他平台去掉即可。

**常驻运行**：

```bash
docker run -d --name embykeeper \
  --restart unless-stopped \
  -e TZ=Asia/Shanghai \
  -v $(pwd)/app:/app \
  ghcr.io/cloudsonly/emby-keeper:latest

docker logs -f embykeeper
```

配置放在 `./app/config.toml`，完整模板见 [`config.example.toml`](config.example.toml)。最小可用配置：

```toml
[emby]
time_range = "<11:00AM,11:00PM>"
interval_days = "<7,12>"

[[emby.account]]
url = "https://your-emby.example.com"
username = "your_username"
password = "your_password"
time = [300, 600]

[llm]
mode = "local"
api_key = "sk-xxxxxxxx"
base_url = "https://open.bigmodel.cn/api/paas/v4"
model = "glm-4-flash"
vision_model = "glm-4v-flash"

[checkiner]
time_range = "<11:00AM,11:00PM>"

[[telegram.account]]
phone = "+8613800000000"
checkiner = true
skip_auth = true
```

`skip_auth = true` 时 `[llm]` 段必填，否则考核问答、看图选按钮、图片验证码全部无解。

**客户端信息全局**：`device` / `client` / `client_version` / `useragent` / `device_id` 可以在 `[emby]` 段统一配置，账号未单独指定时自动继承；账号里写了的那一项优先级更高（例如个别账号要 `device = "Pixel 7 Pro"`）：

```toml
[emby]
concurrency = 3
device         = "Pixel 7"
client         = "Fileball"
client_version = "1.2.3"
useragent      = "Fileball/1.2.3"
device_id      = "0123456789abcdef0123456789abcdef"

[[emby.account]]
url = "https://a.example.com"
username = "u1"
password = "p1"
time = [300, 600]

[[emby.account]]
url = "https://b.example.com"
username = "u2"
password = "p2"
device = "Pixel 7 Pro"   # 只覆盖这一项, 其余仍继承全局
```

五行全部留空也是合法的：程序会按「域名 + 用户名」生成一份指纹，并缓存在 `app/cache.json` 的 `emby.env.<域名>.<用户名>` 下，重启后保持不变。`[subsonic]` 段同样支持 `client` / `client_version` / `useragent` 三个全局值。

---

## 相对上游的改动

| 能力 | 上游行为 | 本 fork |
|---|---|---|
| **总认证** | 启动即连 `@embykeeper_auth_bot` 校验身份 | `skip_auth = true` 走本地实现，完全不连 |
| **additional_auth** | prime / super / captcha / ocr 站点需云端预检 | `[llm].skip_additional_auth = true` 无条件跳过预检 |
| **验证码 / 考核问答** | 云端打码与 GPT | 本地 OpenAI 兼容接口（OpenAI / DeepSeek / 智谱 / 本地模型） |
| **Emby 保活** | 标准 API 假设 | 分块限流、错误码 520–525、`PositionTicks` 夹 int4、租约恢复、base path 探测、CDN 取流、进度上报降级 |
| **TLS 校验** | 强制校验 | 全局 `emby.verify` + 账号级覆盖，兼容自签证书 |
| **网页签到** | 无 | 移植 `plusemby`，含 Turnstile 求解 |
| **群组签到** | `templ_b` 的 `is_chat` / `wait_response` 已支持「群里发关键字」；但模板里写 `chat_name` 会被静默忽略，模板 A 无法在群里用，私有群数字 ID 也匹配不到 | 新增 `chat_name`（群里跟机器人对话，保留机器人过滤）、模板 A 群签到、站点名支持 `-100...` 数字 ID |
| **按钮定位** | 只按文本包含匹配，文案一变就报「账户错误」 | 新增 `bot_checkin_button_index`（按位置）与 `bot_checkin_button_pat`（按正则），失败时打印带序号的面板按钮；`bot_checkin_button_repeat` + `bot_checkin_button_interval` 支持按间隔连点直到成功 |
| **结果关键词** | 只做子串匹配 | 支持 `re:` 前缀写正则，应对每次随机生成的吉祥话回执 |

合并来源与逐项取舍见 [FORK.md](FORK.md)：

[aprpure](https://github.com/aprpure/emby-keeper)：基底，本地 LLM 与 `skip_auth`

[supermoon0201](https://github.com/supermoon0201/emby-keeper)：Emby 畸形站点适配

[Elykia093](https://github.com/Elykia093/emby-keeper)：网页签到器

[ignimutos](https://github.com/ignimutos/emby-keeper)：调度与校验器修复

---

## 镜像

| Tag | 触发方式 | 说明 |
|---|---|---|
| `latest` | 手动触发 `Docker Image Publish` | **生产部署用这个** |
| `main-dev` | push `main` 自动触发 | 开发镜像，**必须挂 `/src`**，否则启动即退出 |

`linux/amd64` 与 `linux/arm64` 双架构，公开可直接拉取，无需登录。

---

## 进阶配置

**网页面板**（`EK_WEBPASS` 非空即启动，容器内固定监听 **1818**）：

```bash
docker run -d --name embykeeper -v $(pwd)/app:/app --net=host \
  -e EK_WEBPASS='你的密码' --restart unless-stopped \
  ghcr.io/cloudsonly/emby-keeper:latest
```

访问 `http://服务器IP:1818`。本镜像只有 `EK_WEBPASS`，没有 `EK_TOKEN`；面板与 `-i` 向导互斥，首次登录 Telegram 请先不带密码跑一次 `-i`。

**网页签到**（该站默认 `__ignore__`，必须显式加入）：

```toml
[telegram.account.site]
checkiner = ["all", "+plusemby"]
```

**多个 plusemby 站点**：一个站点名只对应一份配置（`get_site_config` 返回单个 dict，写成 `[[checkiner.plusemby]]` 数组会报 `'list' object has no attribute 'get'`），所以多站点要按 Telegram 账号隔离：

```toml
[[telegram.account]]
phone = "+8613800000001"

[telegram.account.site]
checkiner = ["plusemby"]

[telegram.account.checkiner_config.plusemby]
url = "https://a.example.com"
username = "user_a"
password = "pass_a"
```

注意 `checkiner_config` 是**整体替换**而非合并：一旦设置，该账号就不再继承全局 `[checkiner]` 的 `time_range` / `timeout` / `retries`，需要在这段里重写一遍。

**需要多级菜单才能签到的机器人**（如 Ask Ash：先点「用户功能」，再点「数据存档」）：

```toml
[checkiner."templ_a<ashUniversal_Bot>"]
name = "Ask Ash"
bot_checkin_path = ["用户功能", "数据存档"]
```

`bot_checkin_path` 会按顺序点击按钮（内联按钮或底部回复键盘都支持），最后一级视为签到按钮；不配置时仍按原来的「在同一条消息里直接找签到按钮」逻辑，找不到会报「账户错误」。

**签到按钮文案每天变**：

```toml
[checkiner."templ_a<ashUniversal_Bot>"]
name = "Ask"
templ_panel_keywords = ["用户面板"]      # 先认出面板消息, 不然每条带按钮的消息都会去试
bot_checkin_path = ["用户功能"]           # 进面板那一级
bot_checkin_button_index = 8             # 面板里第 8 个按钮 (从左到右、从上到下, 1 起)
```

`bot_checkin_button_index` 也支持行列坐标：`[4, 2]` 表示第 4 行第 2 列（同样 1 起）。

**配了位置或正则，`templ_panel_keywords` 就可以不写。** 只要消息上能按位置/正则定位到签到按钮，这条消息就当面板处理——不必再去猜面板文案（它同样会变）。反过来，关键词命中了但那条消息上没有签到按钮（比如进面板前那层「请选择功能」菜单），只会打日志，不会判失败。

定位优先级是 **位置 > 正则 > 文本**：

| 配置 | 适用 |
|---|---|
| `bot_checkin_button_index = 8` | 文案随便变，位置固定（最省事） |
| `bot_checkin_button_pat = "^今朝"` | 文案会变但规律可循 |
| `bot_checkin_button = ["签到", "簽到"]` | 文案固定（默认） |

找不到按钮时会把整块面板的按钮按序号打出来，形如 `1.兑换商店 | 2.删除账号 | ... | 8.今朝已至`，照着填 `bot_checkin_button_index` 即可。

**需要连点几次才成功的机器人**（点一下弹个确认框，确认后再点，往复几次）：

```toml
[checkiner."templ_a<ashUniversal_Bot>"]
name = "Ask"
bot_checkin_button_index = [2, 1]   # /start 面板上那个每天换词的按钮
bot_checkin_button_repeat = 6       # 最多连点 6 次, 出现成功消息就提前结束
bot_checkin_button_interval = 0.6   # 两次点击间隔 (秒), 默认 0.6
```

`bot_checkin_button_repeat` 默认是 `1`，也就是只点一次。调大之后：

- **连点在同一条消息上循环完成**，不是「点一次等一条新消息」。这类机器人点一下往往只弹一个原生弹窗（`BotCallbackAnswer.alert`）或悄悄改一下消息，根本不会有新消息到达，按「等新消息」的写法只会静默超时；
- 每次点击弹出新消息时，会继续按同一个位置点；一旦机器人发了新消息（确认框），那条消息会接管流程，旧面板立即停手，避免两条流程互相抢着点；
- 弹出的确认框如果只有一个按钮（位置对不上），就直接点它；
- 连点过程中的过场提示（如「稳如老狗，奖励稳稳的！」）会被忽略，不会送进智能回答——否则模型一旦点了按钮就会以「避免风险」中止签到；
- 次数用完后不再点击，只从消息文字里认结果。

`bot_checkin_button_interval` 控制两次点击之间的间隔，默认 `0.6` 秒。这个值跟机器人的节流阈值直接相关：间隔太短，有的机器人会直接丢弃点击；间隔太长，有的机器人会弹「操作太频繁」的原生弹窗。知道阈值时按阈值附近调（比如阈值 0.5s 就填 `0.6`）。

`bot_checkin_path` 每一级点击的回调提示，只有在含「成功 / 失败 / 已签到」或「请回答」这类字样时才会被当成结果处理；像进菜单时那句 `✅ 用户界面` 会被静默忽略。否则它会落到「无法识别 → 智能回答」的兜底分支，而该分支一旦让模型点了按钮，就会以「避免风险」为由中止整个签到。

**群组签到**（有些站点只在群里响应，私聊机器人没反应）：

| 场景 | 模板 | 关键配置 | 来源 |
|---|---|---|---|
| 群里发个关键字就算签到 | `templ_b` | `is_chat` | **上游原生** |
| 群里发个关键字，发完即结束（不等回复） | `templ_b` | `is_chat` + `wait_response = false` | **上游原生** |
| 群里 @ 机器人发命令，等它回「签到成功」 | `templ_b` | `chat_name` | 本 fork |
| 群里点按钮 / 走面板签到 | `templ_a` | `chat_name` | 本 fork |
| 私有群（无 `@username`） | 任意 | 站点名写数字 ID | 本 fork |

最常见的「群里发个关键字，发完即结束」是上游原生能力，**不需要本 fork 的任何改动**：

```toml
[checkiner.'templ_b<some_emby_group>']
name = "某某群"
is_chat = true           # 尖括号里写群, 不是机器人
bot_checkin_cmd = "签到"
wait_response = false    # 发完直接记为成功并结束
bot_use_captcha = false  # 群里图片多, 关掉 OCR
```

`bot_checkin_cmd` 给列表时每次随机挑一个：`["签到", "打卡"]`。

本 fork 补的是另一条路径——**在群里跟机器人对话**（`chat_name`），它保留 `filters.user(机器人)`，只处理机器人的回复，群友的闲聊不会被当成签到结果；模板 A 也因此能在群里点按钮签到：

```toml
[checkiner.'templ_b<SomeEmbyBot>']
name = "某某群"
chat_name = "some_emby_group"             # 群的 @username, 或私有群数字 ID
bot_checkin_cmd = "/checkin@SomeEmbyBot"  # 群里发命令必须带 @机器人名
bot_success_keywords = ["签到成功"]
```

`chat_name` 与 `is_chat` 的区别：前者**保留**机器人过滤，只认机器人的回复；后者把尖括号里的会话直接当群，没有机器人可过滤，群里任何人说「成功」都可能被判签到成功，所以 `is_chat` 模式下务必用 `bot_success_keywords` 收窄。

私有群 / 频道只能用数字 ID，站点名里也支持：`[checkiner.'templ_b<-1001234567890>']`（自动转成 int，写字符串会被 pyrogram 当用户名去解析）。上游正则 `templ_(\w+)<@?(\w+)>` 匹配不到带 `-` 的 ID，这是本 fork 放宽的。若 `chat_name` 与站点名里的会话同名，过滤器会变成「群组发的消息 + 发信人是群组本身」，永远匹配不到，程序会打一条警告提醒改用 `is_chat`。

已有内置群签到器可直接启用（都需显式加入白名单）：`pandatv_group`、`akile_group`、`bytevirt_group`、`epub_group`、`epub_group_chat`、`feiyuemusic_group_old`、`marmot_group_old`、`pornfans_group`、`pornfans_game_group`。其中 `pandatv_group` 属于默认启用范围，其余在 `__ignore__` 里。

**MICU**已内置算术题处理：自动解析 `8 - 5 = ?`、在选项按钮里找等值项并点击，同时关掉了图片 OCR 和按钮 toast 解析。无需额外配置。

**机器人先出算术题/验证题**（其他站点）：这类提示常写成「签到成功，请回答 5+5=?」，直接按成功关键词判定就会漏答并误报成功。内置已覆盖「请回答 / 请计算 / 请输入答案 / 请选择正确答案 / 回答下列 / 请作答 / 请完成下面 / 这道题 / 答错 / 重新生成 / 补全诗句 / 依次点击」，并用算式正则兜底（含全角符号），命中后交给智能回答（`[llm]`），答完继续等待真正的成功消息。题目以「配图 + 文字按钮」形式发来的（如 `@niubi2233_bot` 的「防機器签到验证」）也不会再被当成验证码图片去 OCR。

需要**按顺序点多个按钮**的题目（如补全诗句：「忆对中秋丹桂＿。花＿杯中。」→ 先点「丛」再点「在」）会解析回答里的全部 `[CLICK]^XXX^` 并按顺序点击，每次点击间隔 0.8–2 秒；只要有一个目标匹配不上就整体放弃重试，不会只点对一半把题目做废。诗句类题目还会给模型追加「挖空位置 + 按顺序作答」的专用提示，模型若直接回字（如「起足」）也会按字符匹配到按钮。

**一处空缺要补几个字，是这类题最容易翻车的地方。** 占位符（`░`、`＿`、`□`）的宽度不代表字数：`乃者兵░是凶器` 只画了一个 `░`，实际要补两个字。所以：

- 程序会先数题面里占位符的个数，多个空位时在提示词里写死「一共挖空了 N 个字，必须输出 N 个 `[CLICK]`」；只有一个占位符时则提示「占位符宽度不代表字数，按实际缺的字逐字输出」；
- 机器人抱怨「**空位连在一起了没有补全，至少要按顺序点击两个答案按钮**」时，不会被当成异常返回信息（旧行为是交给智能回答 → 模型又点一次 → 以「避免风险」中止整个签到），而是带着**它的原话**和**上次点过的字**重新作答，最多重答 2 次；
- 重答时机器人若已重建键盘导致旧按钮失效，点击异常只记 debug 日志，不会让整次签到判失败。

**「签到中...」这类中间状态不会被当成异常返回信息**：`@laoyou_bot`、`@lgembybot` 收到 `/checkin` 后先回一条 `签到中...`，几秒后才回 `🎉 签到成功! 获得 21 积分`。旧实现把前者当成异常返回信息交给智能回答，模型答不出状态消息，连续 3 次拿不到答案就以「避免风险签到器将停止」判失败——而真正的成功消息随后才到，已经没人处理了。现在命中「签到中 / 正在签到 / 正在处理 / 排队中 / 请稍候」这类模式只记一条 debug 日志，继续等结果；该判断排在成功/失败关键词**之后**，所以「签到成功, 正在处理奖励」仍按成功处理。站点措辞不同时用 `bot_in_progress_keywords` 覆盖内置模式。

**「请完成…验证」这类提示不会被当成签到成功**：机器人常把 `请完成诗句填空进行验证` 单独发一条，里面的「完成」正好命中成功关键词。现在这类指令文本会被识别为验证题（`请/需 + 完成/进行 + 验证/校验/填空/答题/作答`），作答期间到达的成功消息会**暂存**并在作答结束后补处理（机器人往往在答对的瞬间就发结果，那时智能回答还没返回），避免既误判成功、又白等到超时。

**带说明文字的签到结果不会被当成验证码**：`✅ 验证通过 / 🐮 签到成功` 这类结果卡片如果带图，旧实现会拿去做 OCR 并把乱码当成答案发出去；现在说明文字里含成功/失败/已签到关键词的图片一律按文字处理。另外，文档、视频、动图等非图片媒体的说明文字以前会被静默丢弃（只认 `.photo`），导致结果消息永远等不到，现在也会正常解析。

站点措辞不同就补关键词：

```toml
[checkiner.micu]
bot_challenge_keywords = ["请回答", "算一下"]
```

**回执文案每次都随机**时，关键词可以写正则（以 `re:` 开头，其余仍按子串匹配）：

```toml
[checkiner.'templ_a<ashUniversal_Bot>']
bot_success_keywords = ["re:奖励|红包|到账|恭喜"]
```

这条规则对 `bot_success_keywords` / `bot_fail_keywords` / `bot_checked_keywords` / `bot_challenge_keywords` / `bot_account_fail_keywords` / `bot_text_ignore` 都生效。写错的正则会被当作不匹配跳过，不会让签到崩掉。

若机器人把「签到成功」放在按钮回调的 toast 里、题目在随后一条消息，关掉 toast 解析即可：

```toml
[checkiner.micu]
use_button_answer = false
```

**畸形站点**（按站点逐个开，不要一次全开）：

```toml
[[emby.account]]
stream_workaround = true          # Range / 分块读取被拒
external_stream_workaround = true # 外部流媒体地址同样兼容
stream_cdn_path = true            # 走 CDN 取流
progress_fallback = true          # 进度上报失败时只发结束事件
allow_stream = true               # 取不到片长仍播放（约 10 分钟）
verify = true                     # 单独开启 TLS 校验
```

**Turnstile 求解**（月饼 embyguard 等）：

```toml
[turnstile]
solver_url = "http://127.0.0.1:8889"
# 或
yescaptcha_key = ""
```

**本地 helper**（承接 captcha / wssocks / Telegram 推送等 LLM 做不了的能力）：

```toml
[llm]
helper_command = "python /app/helper.py"
helper_timeout = 120
```

协议：stdin 单行 JSON、stdout 单行 JSON、必须 exit 0。示例见 [`helper/helper.py`](helper/helper.py)。

---

## 支持的站点

README 不维护站点清单——站点关服频繁，静态列表必然过时。以源码为准：

| 模块 | 目录 |
|---|---|
| 每日签到 | `embykeeper/telegram/checkiner/` |
| 定时抢注 | `embykeeper/telegram/registrar/` |
| 群组监控 | `embykeeper/telegram/monitor/` |
| 自动水群 | `embykeeper/telegram/messager/` |

文件里标了 `__ignore__ = True` 的站点默认禁用，需用 `+站点名` 显式启用。

---

## 声明

本项目涉及的一切 Emby 服务器与 Embykeeper 开发团队无关，使用 Embykeeper 造成的一切损失（包括但不限于 Emby 或 Telegram 账号被封禁或被群封禁）与开发团队无关。为账号安全，推荐使用小号。运行该工具的 Telegram 账号若通过接码注册，请使用一段时间再接入本工具。

本项目设计初衷是在中文 Emby 社群规则下，保号要求逐渐苛刻（部分要求每月登录或每日签到），使得休闲时间紧张的人士难以安心使用。本项目仅旨在帮助该类人群保号，不鼓励持有大量 Emby 账号而不使用、导致真正需要的人难以获得账号的行为，开发团队也呼吁仅保留 1-2 个较全面、质量较高的 Emby 服务器。本项目仅提供工具，具体使用形式及造成的影响和后果与开发团队无关。

当您安装并使用该工具，默认您已经阅读并同意上述声明，并确认自己并非出于"集邮"目的而安装。

## 致谢

核心功能全部由上游团队完成：[jackzzs](https://github.com/jackzzs)（原作者）、[zetxtech](https://github.com/zetxtech)（主要维护者）。本 fork 合并的改进见上方"相对上游的改动"。

如果这个工具对你有帮助，请优先支持上游：[爱发电](https://afdian.com/a/jackzzs)。

## 许可

与上游一致，见 [LICENSE](LICENSE)。
