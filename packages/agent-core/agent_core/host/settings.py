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

# 类型化设置（D3+）：补全参数，与字符串三件套共享同一个 settings.json。
# 它们不能套用「空串=不改」的规则 —— 0 / False 都是合法值，必须按「显式存在」判断。
_BOOL_KEYS: tuple[str, ...] = ("fim_enabled",)
_INT_KEYS: tuple[str, ...] = (
    "completion_max_tokens",
    "completion_prefix_chars",
    "completion_suffix_chars",
)
_TYPED_KEYS: tuple[str, ...] = _BOOL_KEYS + _INT_KEYS

# 字段 -> 对应的环境变量名（环境变量恒为最高优先级）。
_ENV_FOR_FIELD: dict[str, str] = {
    "model": "VIBE_MODEL",
    "base_url": "VIBE_BASE_URL",
    "api_key": "VIBE_API_KEY",
}
_ENV_FOR_TYPED: dict[str, str] = {
    "fim_enabled": "VIBE_FIM",
    "completion_max_tokens": "VIBE_COMPLETION_MAX_TOKENS",
    "completion_prefix_chars": "VIBE_COMPLETION_PREFIX_CHARS",
    "completion_suffix_chars": "VIBE_COMPLETION_SUFFIX_CHARS",
}


def settings_path() -> Path:
    """设置文件路径：``VIBE_SETTINGS`` 优先，否则 ``~/.vibe/settings.json``。"""
    override = (os.environ.get("VIBE_SETTINGS") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe" / "settings.json"


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """读取设置。读不到 / 坏 JSON / 类型不对一律回空 dict —— 绝不拦服务启动。

    与 ``SessionStore`` 的「坏库自愈」同一纪律：设置文件坏了只是「回到默认」，
    不该让整个后端起不来。类型化键严格校验（bool 只收 JSON bool；int 只收
    非负整数且拒绝 bool 混入），非法值**逐键忽略**而非整份作废。
    """
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key in _SETTINGS_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    for key in _BOOL_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):
            result[key] = value
    for key in _INT_KEYS:
        value = raw.get(key)
        # bool 是 int 的子类，必须显式排除，否则 True 会被当成 1 落盘
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


def save_settings(patch: Mapping[str, Any], path: Path | None = None) -> dict[str, Any]:
    """合并写入设置（只认白名单键）。

    字符串键沿用「空字符串 = 不修改」（界面里 apiKey 留空表示「保持原值」）；
    类型化键按「**显式存在且值合法**」落盘 —— `0` / `False` 是合法值，不能被
    空串规则误伤。写入用「同目录临时文件 + ``os.replace``」保证原子性：
    中途失败不会留下半截 JSON。
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
    for key in _BOOL_KEYS:
        if key in patch and isinstance(patch[key], bool):
            current[key] = patch[key]
    for key in _INT_KEYS:
        value = patch.get(key)
        if key in patch and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            current[key] = value
    document: dict[str, Any] = {"version": 1, **current}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return current


def env_locked_fields(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """哪些模型/补全字段被环境变量锁定（界面应置灰并提示「改这里不生效」）。"""
    source = env if env is not None else os.environ
    mapping = {**_ENV_FOR_FIELD, **_ENV_FOR_TYPED}
    return {
        field: bool(str(source.get(mapping[field]) or "").strip())
        for field in (*_SETTINGS_KEYS, *_TYPED_KEYS)
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


def effective_completion_overrides(
    env: Mapping[str, str],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """算出喂给 ``Config.load(**overrides)`` 的补全参数覆盖项。

    纪律同 ``effective_model_overrides``：环境变量已设 → **不传** override，
    把最高优先级留给 ``Config.load`` 自己；否则用 settings.json 里的合法值。
    传入的 ``settings`` 应是 ``load_settings()`` 的结果（已过类型校验）。
    """
    result: dict[str, Any] = {}
    for field in _TYPED_KEYS:
        if str(env.get(_ENV_FOR_TYPED[field]) or "").strip():
            continue
        if field in settings:
            result[field] = settings[field]
    return result