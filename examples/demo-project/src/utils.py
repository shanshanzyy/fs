"""示例模块：故意留了几个小问题，用来试跑 vibe-agent。

这些「问题」是刻意留的，配合 packages/agent-core/scripts/mock_model_server.py
的默认剧本，可以完整演示一次「读代码 -> 定位 -> 打补丁 -> 人审 -> 落盘」闭环。
"""

from __future__ import annotations

import json
from datetime import datetime


def parse_date(text):
    # 问题 1：没有做空值判断，text 为 None 时抛 TypeError
    return datetime.strptime(text, "%Y-%m-%d")


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def average(values):
    # 问题 2：空列表会 ZeroDivisionError
    return sum(values) / len(values)


def format_user(user):
    # 问题 3：key 不存在时抛 KeyError
    return f"{user['name']} <{user['email']}>"


if __name__ == "__main__":
    print(parse_date("2026-09-23"))
    print(average([1, 2, 3]))
