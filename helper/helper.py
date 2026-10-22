#!/usr/bin/env python3
"""Embykeeper 本地 helper 模板（配合 aprpure fork 的 skip_auth = true）。

协议
----
stdin : 一行 JSON  {"method": "...", "instance": "<tg_user_id>", ...}
stdout: 一行 JSON  {"answer": ...} / {"success": true} / {"status": "error", "errmsg": "..."}
stderr: 随便打日志（不会影响解析）
退出码: 必须是 0，非 0 会被判定为 helper 执行失败

约定
----
1. 只有 stdout 的第一段内容会被 json.loads；解析失败时整段字符串会被当作结果返回。
   所以任何调试输出都必须走 stderr，不能 print 到 stdout。
2. 每次调用都会新起一个进程，保持轻量，别在这里做重初始化。
3. 未实现的方法返回 {"status": "error"}，调用方会退化成「该能力不可用」。

依赖：仅标准库（urllib）。容器内 `python` 指向 /opt/venv/bin/python。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 配置：全部走环境变量，避免把密钥写进代码
# ---------------------------------------------------------------------------
TG_TOKEN = os.getenv("HELPER_TG_TOKEN", "")
TG_CHAT = os.getenv("HELPER_TG_CHAT", "")
TG_API = os.getenv("HELPER_TG_API", "https://api.telegram.org")

LLM_KEY = os.getenv("HELPER_LLM_KEY", "")
LLM_BASE = os.getenv("HELPER_LLM_BASE", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("HELPER_LLM_MODEL", "gpt-4o-mini")
LLM_VISION = os.getenv("HELPER_LLM_VISION", "") or LLM_MODEL
HTTP_TIMEOUT = float(os.getenv("HELPER_HTTP_TIMEOUT", "30"))
PROXY = os.getenv("HELPER_PROXY", "") or os.getenv("HTTPS_PROXY", "") or os.getenv("https_proxy", "")


def log(msg: str) -> None:
    print(f"[helper] {msg}", file=sys.stderr)


def _opener():
    if PROXY:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener()


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = HTTP_TIMEOUT):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with _opener().open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 能力实现
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> bool:
    """把消息转发到自己的 Telegram 机器人。"""
    if not (TG_TOKEN and TG_CHAT):
        log("未配置 HELPER_TG_TOKEN / HELPER_TG_CHAT，消息只写 stderr")
        return False
    try:
        post_json(
            f"{TG_API}/bot{TG_TOKEN}/sendMessage",
            {"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
        )
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Telegram 推送失败: {type(e).__name__}: {e}")
        return False


def send_webhook(text: str) -> bool:
    """通用 webhook（Bark / Server 酱 / 自建接口），HELPER_WEBHOOK 里写完整 URL。"""
    url = os.getenv("HELPER_WEBHOOK", "")
    if not url:
        return False
    try:
        post_json(url, {"title": "Embykeeper", "text": text})
        return True
    except Exception as e:  # noqa: BLE001
        log(f"webhook 推送失败: {type(e).__name__}: {e}")
        return False


def llm_chat(messages: list, model: str | None = None) -> str | None:
    """OpenAI 兼容 chat completion。"""
    if not LLM_KEY:
        return None
    try:
        data = post_json(
            f"{LLM_BASE}/chat/completions",
            {
                "model": model or LLM_MODEL,
                "messages": messages,
                "max_tokens": 512,
                "temperature": 0.1,
            },
            headers={"Authorization": f"Bearer {LLM_KEY}"},
        )
        return (data["choices"][0]["message"].get("content") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        log(f"LLM 请求失败: {type(e).__name__}: {e}")
        return None


def do_ocr(photo_b64: str) -> dict:
    answer = llm_chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "只输出图片中的字符，不要任何解释。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo_b64}"}},
                ],
            }
        ],
        model=LLM_VISION,
    )
    if answer:
        return {"answer": answer}
    return {"status": "error", "errmsg": "OCR 未实现：请配置 HELPER_LLM_KEY 或接入自己的打码服务"}


def do_visual(photo_b64: str, options: list, question: str | None) -> dict:
    prompt = f"从以下选项中选择与图片最匹配的一个，只输出选项原文：{', '.join(options)}"
    if question:
        prompt = f"{question}\n{prompt}"
    answer = llm_chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo_b64}"}},
                ],
            }
        ],
        model=LLM_VISION,
    )
    return {"answer": answer, "by": "helper"} if answer else {"status": "error", "errmsg": "视觉识别失败"}


def handle(req: dict) -> dict:
    method = req.get("method", "")
    instance = req.get("instance", "")

    # ---- 推送类：helper 是唯一通道，最值得实现 ----
    if method in ("send_log", "send_msg"):
        text = req.get("message") or ""
        ok = send_telegram(text) or send_webhook(text)
        if not ok:
            log(f"消息未发出（{method}, instance={instance}）: {text[:120]}")
        # 返回 success=True 表示"已处理"，避免调用方再打一条本地日志
        return {"success": ok}

    # ---- 文本问答类：LLM 已配时会先走 LLM，这里只是兜底 ----
    if method == "gpt":
        answer = llm_chat([{"role": "user", "content": req.get("prompt", "")}])
        return {"answer": answer, "by": "helper"} if answer else {"status": "error", "errmsg": "gpt 失败"}

    if method == "terminus_answer":
        answer = llm_chat(
            [
                {
                    "role": "user",
                    "content": "回答下面的考试题，直接给答案，不要解释：\n\n" + (req.get("question") or ""),
                }
            ]
        )
        return {"answer": answer, "by": "helper"} if answer else {"status": "error", "errmsg": "答题失败"}

    if method == "pornemby_answer":
        answer = llm_chat(
            [
                {
                    "role": "user",
                    "content": "回答下面的单选题，只输出 A/B/C/D 其中一个字母：\n\n"
                    + (req.get("question") or ""),
                }
            ]
        )
        return {"answer": answer, "by": "helper"} if answer else {"status": "error", "errmsg": "答题失败"}

    # ---- 视觉类 ----
    if method == "ocr":
        return do_ocr(req.get("photo_base64", ""))

    if method == "visual":
        return do_visual(
            req.get("photo_base64", ""),
            req.get("options") or [],
            req.get("question"),
        )

    # ---- 上游 auth bot 的专有能力，需要自己接服务 ----
    if method == "captcha":
        # 入参: site, url(可选)
        # 期望: {"token": "..."} —— 该站点验证码的应答令牌
        return {"status": "error", "errmsg": "captcha 未实现"}

    if method == "captcha_content":
        # 入参: site, url(可选)
        # 期望: {"content": "..."} —— 需要复制回复的网页正文
        return {"status": "error", "errmsg": "captcha_content 未实现"}

    if method == "cf_clearance":
        # 入参: url
        # 期望: {"cf_clearance": "...", "useragent": "..."}
        return {"status": "error", "errmsg": "cf_clearance 未实现"}

    if method == "wssocks":
        # 入参: 无
        # 期望: {"url": "wss://...", "token": "..."}
        return {"status": "error", "errmsg": "wssocks 未实现"}

    if method == "captcha_wssocks":
        # 入参: token, url, user_agent
        # 期望: {"cf_clearance": "...", "useragent": "..."}
        return {"status": "error", "errmsg": "captcha_wssocks 未实现"}

    return {"status": "error", "errmsg": f"未知方法: {method}"}


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"status": "error", "errmsg": "stdin 为空"}, ensure_ascii=False))
        return 0
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "errmsg": f"请求不是合法 JSON: {e}"}, ensure_ascii=False))
        return 0

    try:
        result = handle(req)
    except Exception as e:  # noqa: BLE001
        log(f"未捕获异常: {type(e).__name__}: {e}")
        result = {"status": "error", "errmsg": f"{type(e).__name__}: {e}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
