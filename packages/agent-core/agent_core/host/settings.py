"""用户级模型设置（单一真源）。

背景：模型名 / 接入地址 / API Key 原先只能靠环境变量或桌面壳的 config.json，
用户想在界面里改就得手改文件。这里把「界面里保存的模型配置」落成一个**用户级**
文件（默认 ``~/.vibe/settings.json``，可用环境变量 ``VIBE_SETTINGS`` 覆盖路径），
与工作区无关 —— 换项目不必重配模型。

优先级（高到低）：**环境变量 > settings.json > 命令行参数 > 内核默认**。
桌面壳的 config.json 依旧通过 ``--model/--base-url/--api-key`` 传进来，落在
「命令行参数」这一层 —— 因此界面保存的值会盖过它，这正是本特性想要的行为。

为什么必须在这里算优先级、而不是直接把这些值塞进 ``Config.load(**overrides)``：
``Config.load`` 先用环境变量建配置、最后才 ``replace(**overrides)``，overrides 会
**盖过**环境变量。要让环境变量保持最高优先级，就只能「环境变量已设置时不给该字段
传 override」，把决定权留给 ``Config.load`` 自己。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

# 允许持久化的字段（白名单）。解释器路径属桌面壳专有，不进这里。
_SETTINGS_KEYS: tuple[str, ...] = ("model", "base_url", "api_key")

# 字段 -> 对应的环境变量名（环境变量恒为最高优先级）。
_ENV_FOR_FIELD: dict[str, str] = {
    "model": "VIBE_MODEL",
    "base_url": "VIBE_BASE_URL",
    "api_key": "VIBE_API_KEY",
}


def settings_path() -> Path:
    """设置文件路径：``VIBE_SETTINGS`` 优先，否则 ``~/.vibe/settings.json``。"""
    override = (os.environ.get("VIBE_SETTINGS") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe" / "settings.json"


def load_settings(path: Path | None = None) -> dict[str, str]:
    """读取设置。读不到 / 坏 JSON / 类型不对一律回空 dict —— 绝不拦服务启动。

    与 ``SessionStore`` 的「坏库自愈」同一纪律：设置文件坏了只是「回到默认」，
    不该让整个后端起不来。
    """
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for key in _SETTINGS_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return result


def save_settings(patch: Mapping[str, Any], path: Path | None = None) -> dict[str, str]:
    """合并写入设置（只认白名单键，非空字符串才落盘）。

    空字符串 = 不修改（界面里 apiKey 留空表示「保持原值」）。
    写入用「同目录临时文件 + ``os.replace``」保证原子性：中途失败不会留下半截 JSON。
    """
    target = path or settings_path()
    current = load_settings(target)
    for key in _SETTINGS_KEYS:
        if key not in patch:
            continue
        value = patch.get(key)
        text = "" if value is None else str(value).strip()
        if text:
            current[key] = text
    document: dict[str, Any] = {"version": 1, **current}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return current


def env_locked_fields(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """哪些模型字段被环境变量锁定（界面应置灰并提示「改这里不生效」）。"""
    source = env if env is not None else os.environ
    return {
        field: bool(str(source.get(_ENV_FOR_FIELD[field]) or "").strip())
        for field in _SETTINGS_KEYS
    }


def effective_model_overrides(
    env: Mapping[str, str],
    settings: Mapping[str, str],
    cli: Mapping[str, Any],
) -> dict[str, str]:
    """算出喂给 ``Config.load(**overrides)`` 的模型字段覆盖项。

    逐字段按 ``环境变量 > settings.json > 命令行 > 默认`` 取舍：
    - 环境变量已设置 → **不传** override（让 ``Config.load`` 自己读，保住最高优先级）；
    - 否则 settings.json 有值 → 用它；
    - 否则命令行参数有值 → 用它；
    - 都没有 → 不传（用内核默认）。
    """
    result: dict[str, str] = {}
    for field in _SETTINGS_KEYS:
        if str(env.get(_ENV_FOR_FIELD[field]) or "").strip():
            continue
        value = str(settings.get(field) or "").strip() or str(cli.get(field) or "").strip()
        if not value:
            continue
        result[field] = value.rstrip("/") if field == "base_url" else value
    return result