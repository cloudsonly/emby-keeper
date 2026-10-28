"""签到汇总日志的回归测试.

背景: 终点站的图片验证题面 ("请在 30 秒内点击图中事物的按钮以完成签到 / 每天只有
一次机会, 失败或者过期当天不可重试") 同时含 "完成" 与 "失败", 被当成签到结果后立刻
retry; 重试时机器人回 "今日已经签到过了", 最终状态落成 NONEED. 汇总里只剩一个
"已签到而跳过" 的计数, 推送里连站点名字都不出现, 用户完全看不出它出过问题.
"""

import asyncio

from embykeeper.runinfo import RunStatus
from embykeeper.telegram.checkin_main import CheckinerManager


class FakeLog:
    def __init__(self):
        self.lines = []

    def bind(self, **kwargs):
        return self

    def _record(self, level):
        def _inner(message):
            self.lines.append((level, message))

        return _inner

    @property
    def error(self):
        return self._record("error")

    @property
    def warning(self):
        return self._record("warning")

    @property
    def info(self):
        return self._record("info")

    def text(self):
        return "\n".join(message for _, message in self.lines)

    def level_of(self, name):
        return [message for level, message in self.lines if level == name]


class FakeCheckiner:
    def __init__(self, name, current_retries=0):
        self.name = name
        self.current_retries = current_retries


class FakeResult:
    def __init__(self, status):
        self.status = status


def summarize(items):
    log = FakeLog()
    results = {str(i): (checkiner, FakeResult(status)) for i, (checkiner, status) in enumerate(items)}
    CheckinerManager._log_batch_summary(results, log)
    return log


def test_retried_then_noneed_is_named_separately():
    """先失败过、重试后落成 NONEED 的站点必须点名, 不能只算进计数."""
    log = summarize(
        [
            (FakeCheckiner("终点站", current_retries=1), RunStatus.NONEED),
            (FakeCheckiner("@ok_bot"), RunStatus.SUCCESS),
        ]
    )

    text = log.text()
    assert "签到成功 (共2个, 1成功, 1已签到而跳过)." in text
    warnings = log.level_of("warning")
    assert len(warnings) == 1
    assert "终点站" in warnings[0]
    assert "首次尝试未成功" in warnings[0]


def test_plain_noneed_is_not_named():
    """本来就是已签到的站点不该被点名, 否则每次跑都会刷出一堆名字."""
    log = summarize(
        [
            (FakeCheckiner("@ok_bot"), RunStatus.NONEED),
            (FakeCheckiner("@ok2_bot"), RunStatus.SUCCESS),
        ]
    )

    assert log.level_of("warning") == []
    assert "首次尝试未成功" not in log.text()


def test_retried_checkiner_is_still_reported_alongside_failures():
    """有失败站点时, 错误行照旧列失败名单, 重试后已签到的另起一行."""
    log = summarize(
        [
            (FakeCheckiner("终点站", current_retries=2), RunStatus.NONEED),
            (FakeCheckiner("@bad_bot"), RunStatus.FAIL),
        ]
    )

    errors = log.level_of("error")
    assert len(errors) == 1
    assert "签到失败 (共2个, 1已签到而跳过, 1失败): @bad_bot" in errors[0]
    warnings = log.level_of("warning")
    assert len(warnings) == 1
    assert "终点站" in warnings[0]


def test_empty_results_emit_nothing():
    log = summarize([])
    assert log.lines == []


def test_summary_is_sync():
    """汇总只拼日志, 不该被写成协程 (调用点在 async 流程里直接调用)."""
    assert not asyncio.iscoroutinefunction(CheckinerManager._log_batch_summary)
