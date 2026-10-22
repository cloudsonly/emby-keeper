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
| 不想用作者的云端认证服务 | 验证码解析、考核问答、看图选按钮改用本地 LLM，不向第三方发送内容 |
| 自建 / 反代 / CDN 的 Emby 站点保活失败 | 补齐畸形站点适配：分块限流、错误码 520–525、租约恢复、CDN 取流、进度降级 |
| 网页签到站点缺失 | 补上纯网页签到（不依赖 Telegram 机器人） |

核心功能仍是两个：**TG 机器人签到**（数十个站点每日签到）与 **Emby 保号**（模拟播放保持账号活跃）；此外提供自动抢注、群组游戏、考核辅助。

基于上游 `main` 分支 fast-forward 演进，同步上游只需一次 `git merge`。只发布容器镜像，不发布 PyPI 包。

---

## 快速开始

**首次运行**（交互式，按提示输入手机验证码）：

```bash
docker run -v $(pwd)/app:/app --rm -it --net=host \
  ghcr.io/cloudsonly/emby-keeper:latest -i
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

---

## 相对上游的改动

| 能力 | 上游行为 | 本 fork |
|---|---|---|
| **总认证** | 启动即连 `@embykeeper_auth_bot` 校验身份 | `skip_auth = true` 走本地实现，完全不连 |
| **additional_auth** | prime / super / captcha / ocr 站点需云端预检 | `[llm].skip_additional_auth = true` 无条件跳过预检 |
| **验证码 / 考核问答** | 云端打码与 GPT | 本地 OpenAI 兼容接口（OpenAI / DeepSeek / 智谱 / 本地模型） |
| **Emby 保活** | 标准 API 假设 | 分块限流、错误码 520–525、`PositionTicks` 夹 int4、租约恢复、base path 探测、CDN 取流、进度上报降级 |
| **TLS 校验** | 强制校验 | 全局 `emby.verify` + 账号级覆盖，兼容自签证书 |
| **网页签到** | 无 | 移植 `結束バンド`（plusemby），含 Turnstile 求解 |

合并来源与逐项取舍见 [FORK.md](FORK.md)：[aprpure](https://github.com/aprpure/emby-keeper)（基底，本地 LLM 与 `skip_auth`）、[supermoon0201](https://github.com/supermoon0201/emby-keeper)（Emby 畸形站点适配）、[Elykia093](https://github.com/Elykia093/emby-keeper)（网页签到器）、[ignimutos](https://github.com/ignimutos/emby-keeper)（调度与校验器修复）。

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

**多个 plusemby（結束バンド）站点**：一个站点名只对应一份配置（`get_site_config` 返回单个 dict，写成 `[[checkiner.plusemby]]` 数组会报 `'list' object has no attribute 'get'`），所以多站点要按 Telegram 账号隔离：

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
