"""Git 集成（P0-C）：只读地读出仓库状态、diff、分支与冲突。

## 为什么是 subprocess 而不是 GitPython

核心链路零强制依赖（`pyproject.toml` 的 `dependencies` 保持 `[]`），而 git 本身
就是装在机器上的外部命令 —— 引一个 Python 包只为了调它，等于白加一个依赖。
这里用固定 argv 的 `subprocess` 直接调，零依赖且行为与用户在终端里看到的一致。

## 三条必须做对的细节

1. **固定 argv + `shell=False`。** ref / path 可能来自模型或前端，任何形式的
   字符串拼接都会变成注入面（`; rm -rf` 之类）。argv 数组从根上消除它。
2. **`scrub_env()` 脱敏。** 与 `tools/shell.py` 同一口径：剥离 TOKEN/SECRET 等
   变量，避免 git 的钩子或 `credential.helper` 把宿主机凭据带进输出。
3. **非仓库一律返回 `{"available": False, "reason": ...}`，绝不抛异常。**
   调用方（工具 / HTTP 端点 / 界面面板）拿到它就展示「不是 git 仓库」，
   而不是让整个界面因为一个预期内的状态炸掉。

## 为什么不用 `-z`

`-z`（NUL 分隔）对畸形路径更稳，但所有字段都变成机器形态，可读性差一大截；
这里的产出**就是要给人看**（面板、模型），所以用 `-c core.quotePath=false`
关掉路径转义，让中文路径原样输出。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .tools.shell import scrub_env

# 单条 git 命令的超时。只读子命令在正常仓库里都在百毫秒级；
# 20 秒还没回来通常意味着索引损坏或网络远端（后续可能引入网络子命令）。
_TIMEOUT = 20

# diff 正文上限。超大 diff 塞进事件流会拖垮界面，也会把模型上下文冲掉。
_MAX_OUTPUT = 400_000

# 空树对象的固定 hash（git 的「什么都没有」状态）。
# 仓库还没有任何提交（unborn HEAD）时，`git diff HEAD` 直接报
# "ambiguous argument 'HEAD'"；用空树当基准，就能把「全部内容都是新的」表达出来。
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# porcelain v1 里的冲突码。这七种都表示「这个文件处在未解决的合并冲突中」，
# 与普通的 M/D 有本质区别 —— 界面上要单独标红。
_CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _git(
    root: Path, argv: Iterable[str], timeout: int = _TIMEOUT
) -> tuple[bool, str, str, str]:
    """跑一条 git 子命令。返回 `(ok, stdout, stderr, reason)`，永不抛异常。"""
    try:
        proc = subprocess.run(  # noqa: S603 — argv 数组 + shell=False，无注入面
            ["git", *argv],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=scrub_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return False, "", "", "未找到 git 可执行文件（未安装或不在 PATH 中）"
    except subprocess.TimeoutExpired:
        return False, "", "", f"git 命令超过 {timeout} 秒未返回"
    except OSError as exc:
        return False, "", "", f"git 启动失败：{exc}"

    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
        return False, proc.stdout or "", proc.stderr or "", (lines[0] if lines else f"git 退出码 {proc.returncode}")
    return True, proc.stdout or "", proc.stderr or "", ""


def _repo_reason(root: Path) -> str:
    """空串 = 是 git 仓库；非空 = 不是，且这句话可以直接展示给用户。"""
    if not root.is_dir():
        return f"工作区路径不是目录：{root}"
    ok, out, _, reason = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if ok and out.strip() == "true":
        return ""
    return reason or "当前工作区不是 git 仓库"


def is_repo(root: str | Path) -> bool:
    return _repo_reason(Path(root)) == ""


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, **extra}


def _int(text: str) -> int:
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- 状态
def _parse_branch(line: str) -> dict[str, Any]:
    """解析 `## ` 那一行。

    会遇到的形态：
        ## main
        ## main...origin/main [ahead 1, behind 2]
        ## main...origin/main [gone]
        ## HEAD (no branch)            ← 分离头指针
        ## No commits yet on main      ← 仓库还没有任何提交
    """
    body = line[3:].strip() if line.startswith("## ") else line.strip()
    info: dict[str, Any] = {
        "branch": body,
        "upstream": "",
        "ahead": 0,
        "behind": 0,
        "detached": False,
        "unborn": False,
    }

    if body.startswith("No commits yet on "):
        info["unborn"] = True
        info["branch"] = body[len("No commits yet on ") :].strip()
        return info
    if body.startswith("HEAD (no branch)") or body == "HEAD":
        info["detached"] = True
        info["branch"] = "HEAD"
        return info
    if "..." not in body:
        return info

    local, rest = body.split("...", 1)
    info["branch"] = local.strip()
    # `[ahead 1, behind 2]` 挂在上游名后面
    if rest.endswith("]") and " [" in rest:
        rest, tail = rest.rsplit(" [", 1)
        for part in tail.rstrip("]").split(","):
            part = part.strip()
            if part.startswith("ahead "):
                info["ahead"] = _int(part[len("ahead ") :])
            elif part.startswith("behind "):
                info["behind"] = _int(part[len("behind ") :])
    info["upstream"] = rest.strip()
    return info


def status(root: str | Path) -> dict[str, Any]:
    """分支 + 改动清单（`git status --porcelain=v1 -b`）。

    条目形状 `{path, index, worktree, status, conflicted, orig}`：
    `orig` 只在重命名/复制时有值（porcelain 的 `R  old -> new` 形态）。
    """
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    ok, out, _, err = _git(root, ["-c", "core.quotePath=false", "status", "--porcelain=v1", "-b"])
    if not ok:
        return _unavailable(err)

    info: dict[str, Any] = {"branch": "", "upstream": "", "ahead": 0, "behind": 0,
                            "detached": False, "unborn": False}
    entries: list[dict[str, Any]] = []
    for line in out.splitlines():
        if line.startswith("## "):
            info = _parse_branch(line)
            continue
        if len(line) < 4:
            continue
        index_code, worktree_code, body = line[0], line[1], line[3:]
        orig = ""
        path = body
        if " -> " in body:  # 重命名/复制：porcelain 给「旧 -> 新」
            orig, path = body.split(" -> ", 1)
        conflicted = (index_code + worktree_code) in _CONFLICT_CODES
        if conflicted:
            # 冲突码（UU/AA/DD…）本身才是信息 —— 单取一位会退化成 "U"，
            # 界面按它着色、模型按它判断「这里还没解决」都会失准。
            code = index_code + worktree_code
        elif index_code == "?" and worktree_code == "?":
            code = "??"
        elif worktree_code not in (" ", "?"):
            # worktree 状态优先：它才是「现在磁盘上是什么样」
            code = worktree_code
        elif index_code != " ":
            code = index_code
        else:
            code = worktree_code
        entries.append(
            {
                "path": path,
                "index": index_code,
                "worktree": worktree_code,
                "status": code,
                "conflicted": conflicted,
                "orig": orig,
            }
        )

    conflicts = [item["path"] for item in entries if item["conflicted"]]
    return {
        "available": True,
        **info,
        "clean": not entries,
        "entries": entries,
        "conflicts": conflicts,
        "counts": {
            "changed": len(entries),
            "untracked": sum(1 for item in entries if item["status"] == "??"),
            "conflicts": len(conflicts),
        },
    }


def unmerged(root: str | Path) -> list[str]:
    """冲突文件清单（`git diff --diff-filter=U`）。非仓库返回空列表。"""
    root = Path(root)
    if _repo_reason(root):
        return []
    ok, out, _, _ = _git(root, ["-c", "core.quotePath=false", "diff", "--name-only", "--diff-filter=U"])
    if not ok:
        return []
    return [line for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------- diff
def _summarize(diff: str) -> tuple[int, int, int]:
    """数出 (文件数, 新增行, 删除行)。"""

    files = 0
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return files, added, removed


def diff(
    root: str | Path,
    ref: str = "HEAD",
    path: str | None = None,
    staged: bool = False,
) -> dict[str, Any]:
    """相对某个基准的 unified diff。

    `staged=True` 时用 `--cached`（暂存区 vs 基准）；`path` 限定单个路径。
    仓库还没有提交时把基准换成**空树**，否则 `git diff HEAD` 会直接报错
    （见 `_EMPTY_TREE` 的说明）。
    """
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason, ref=ref, staged=staged, path=path or "",
                            diff="", truncated=False, files=0, added=0, removed=0)

    ref = (ref or "HEAD").strip() or "HEAD"
    if ref == "HEAD":
        ok, head, _, _ = _git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
        if not ok or not head.strip():
            ref = _EMPTY_TREE

    argv = ["-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff"]
    if staged:
        argv.append("--cached")
    argv.append(ref)
    if path:
        argv.extend(["--", path])

    ok, out, _, err = _git(root, argv)
    if not ok:
        return _unavailable(err, ref=ref, staged=staged, path=path or "",
                            diff="", truncated=False, files=0, added=0, removed=0)

    truncated = len(out) > _MAX_OUTPUT
    body = out[:_MAX_OUTPUT] if truncated else out
    files, added, removed = _summarize(body)
    return {
        "available": True,
        "ref": ref,
        "staged": staged,
        "path": path or "",
        "diff": body,
        "truncated": truncated,
        "files": files,
        "added": added,
        "removed": removed,
    }


# --------------------------------------------------------------------------- 分支与日志
def branches(root: str | Path) -> dict[str, Any]:
    """本地与远端分支清单。`current` 是当前分支名（分离头指针时为空）。"""
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason, branches=[], current="")
    fmt = "%(refname:short)%09%(objectname:short)%09%(upstream:short)%09%(HEAD)"
    ok, out, _, err = _git(root, ["for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes"])
    if not ok:
        return _unavailable(err, branches=[], current="")

    remotes = _remote_names(root)  # 只查一次，别在循环里逐条起进程
    items: list[dict[str, Any]] = []
    current = ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, commit, upstream, head = parts[0], parts[1], parts[2], parts[3]
        # refs/remotes 里的 origin/HEAD 是个符号引用，出现在清单里只会让人困惑
        if name.endswith("/HEAD"):
            continue
        is_current = head.strip() == "*"
        if is_current:
            current = name
        head_part = name.split("/", 1)[0]
        items.append(
            {
                "name": name,
                "commit": commit,
                "upstream": upstream,
                "current": is_current,
                "kind": "remote" if "/" in name and head_part in remotes else "local",
            }
        )
    return {"available": True, "branches": items, "current": current}


def _remote_names(root: Path) -> set[str]:
    ok, out, _, _ = _git(root, ["remote"])
    if not ok:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def log(root: str | Path, limit: int = 10) -> dict[str, Any]:
    """最近若干条提交（`%h` + 标题行）。供 commit message 生成时对齐仓库风格。"""
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason, commits=[])
    limit = max(1, min(int(limit or 10), 50))
    ok, out, _, err = _git(
        root,
        ["-c", "core.quotePath=false", "log", "--no-color", f"--pretty=format:%h%x09%s", "-n", str(limit)],
    )
    if not ok:
        # 没有提交（unborn HEAD）时 log 会失败 —— 那是空历史，不是错误。
        return {"available": True, "commits": [], "reason": err}
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        commits.append({"short": parts[0], "subject": parts[1] if len(parts) > 1 else ""})
    return {"available": True, "commits": commits}


def snapshot(root: str | Path) -> dict[str, Any]:
    """一次取齐面板需要的全部信息（status + branches + log）。

    界面刷新时调一次而不是三次：三次请求之间仓库可能被改，
    拼出来的面板会出现「分支是新的、改动是旧的」这种自相矛盾。
    """
    return {
        "status": status(root),
        "branches": branches(root),
        "log": log(root, 10),
    }


# --------------------------------------------------------------------------- 写操作（B2）
# 全部是「用户本人在面板上点」的动作（ADR-011/016），与 Agent 通道彻底隔离：
# 这些函数只被 host 的用户动作端点调用，**不进任何 Agent 工具面** ——
# 给 Agent 加 git_commit 等于给「改动落盘唯一入口是补丁引擎」开新口子。
#
# 统一返回形状（与只读函数同构，调用方无需分支）：
#   非仓库        → {"available": False, "reason": ...}
#   git 自身失败  → {"available": True, "ok": False, "reason": <stderr 首行>}
#   成功          → {"available": True, "ok": True, "reason": "", "output": ...}
_REFNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MAX_MESSAGE_CHARS = 500
_WRITE_TIMEOUT = 20
# push/pull 走网络远端，20 秒不够；但也不能无限等 —— 卡住的远端要能被放弃。
_NET_TIMEOUT = 120


def _run_op(
    root: Path, argv: Iterable[str], timeout: int = _WRITE_TIMEOUT, **extra: Any
) -> dict[str, Any]:
    """跑一条**写** git 子命令，把退出码翻成 ok/reason（永不抛异常）。"""
    ok, out, _err, why = _git(root, argv, timeout=timeout)
    if not ok:
        return {"available": True, "ok": False, "reason": why, "output": out}
    return {"available": True, "ok": True, "reason": "", "output": out, **extra}


def _has_head(root: Path) -> bool:
    """仓库是否已有提交（unborn HEAD）—— 决定 unstage 走 reset 还是 rm --cached。"""
    ok, out, _, _ = _git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
    return ok and bool(out.strip())


def valid_refname(name: str) -> bool:
    """分支名合法性：白名单正则 + 几条 git refname 硬规则，拒绝怪名注入。

    正则已挡住前导 `-`/`.`/空格；这里再补上 `..`、`@{`、`\\`、`//`、
    结尾 `/`、结尾 `.`、结尾 `.lock` 这些 git 明确禁止的形态。
    """
    text = (name or "").strip()
    if not text or len(text) > 200:
        return False
    if not _REFNAME_RE.match(text):
        return False
    if (
        ".." in text
        or "@{" in text
        or "\\" in text
        or "//" in text
        or text.endswith("/")
        or text.endswith(".")
        or text.endswith(".lock")
    ):
        return False
    return True


def stage(root: str | Path, paths: Iterable[str] | None = None, all: bool = False) -> dict[str, Any]:
    """暂存：`git add -- <paths...>`（all=True 时 `git add -A`）。"""
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    if all:
        return _run_op(root, ["add", "-A"])
    items = [str(p) for p in (paths or []) if str(p).strip()]
    if not items:
        return {"available": True, "ok": False, "reason": "未指定要暂存的路径", "output": ""}
    return _run_op(root, ["add", "--", *items])


def unstage(root: str | Path, paths: Iterable[str] | None = None, all: bool = False) -> dict[str, Any]:
    """取消暂存。

    已提交仓库用 `git reset -q HEAD -- <paths>`；**unborn**（一次提交都还没有）
    时 HEAD 解析不了，改用 `git rm --cached` —— 否则用户在新仓库里第一次
    「取消暂存」必然报错，而这恰恰是最常见的场景。
    """
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    has_head = _has_head(root)
    if all:
        argv = (
            ["reset", "-q", "HEAD"]
            if has_head
            else ["rm", "--cached", "-r", "--quiet", "--", "."]
        )
        return _run_op(root, argv)
    items = [str(p) for p in (paths or []) if str(p).strip()]
    if not items:
        return {"available": True, "ok": False, "reason": "未指定要取消暂存的路径", "output": ""}
    argv = (
        ["reset", "-q", "HEAD", "--", *items]
        if has_head
        else ["rm", "--cached", "--quiet", "--", *items]
    )
    return _run_op(root, argv)


def discard(root: str | Path, paths: Iterable[str] | None = None) -> dict[str, Any]:
    """放弃改动（破坏性）：已跟踪文件 `git checkout --`，未跟踪文件 `git clean -f --`。

    **只做 git，不做检查点** —— 前置检查点由 host 层负责（B5）：检查点属于
    「落盘兜底」机制，依赖工作区与审计，不该塞进这个纯 git 模块。
    """
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    items = [str(p) for p in (paths or []) if str(p).strip()]
    if not items:
        return {"available": True, "ok": False, "reason": "未指定要放弃的路径", "output": ""}
    state = status(root)
    untracked = {
        e.get("path") for e in (state.get("entries") or []) if e.get("status") == "??"
    }
    tracked = [p for p in items if p not in untracked]
    loose = [p for p in items if p in untracked]
    outputs: list[str] = []
    if tracked:
        result = _run_op(root, ["checkout", "--", *tracked])
        if not result["ok"]:
            return result
        outputs.append(str(result.get("output") or ""))
    if loose:
        result = _run_op(root, ["clean", "-f", "--", *loose])
        if not result["ok"]:
            return result
        outputs.append(str(result.get("output") or ""))
    return {
        "available": True,
        "ok": True,
        "reason": "",
        "output": "\n".join(o for o in outputs if o),
    }


def commit(root: str | Path, message: str) -> dict[str, Any]:
    """提交："git commit -m <message>"。

    message 作为**单个 argv 元素**传递：数组天然没有注入面，也不需引号转义。
    空消息 / 超 500 字符拒绝；**刻意不加 `--no-verify`** —— 用户装的钩子
    必须照跑（那正是用户把它装上的目的）。
    """
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    text = (message or "").strip()
    if not text:
        return {"available": True, "ok": False, "reason": "提交信息不能为空", "output": ""}
    if len(text) > _MAX_MESSAGE_CHARS:
        return {
            "available": True,
            "ok": False,
            "reason": f"提交信息过长（上限 {_MAX_MESSAGE_CHARS} 字符）",
            "output": "",
        }
    result = _run_op(root, ["commit", "-m", text])
    if not result["ok"]:
        return result
    oid = ""
    ok, out, _, _ = _git(root, ["rev-parse", "--short", "HEAD"])
    if ok:
        oid = out.strip()
    result["oid"] = oid
    return result


def push(root: str | Path) -> dict[str, Any]:
    """推送当前分支到已设上游（无参 `git push`）。

    不带 `--force`；没有上游时 git 自己报错，原样透出引导用户先在终端设上游
    —— 代设上游 = 替用户决定推哪去，超出「用户本人动作」的边界。
    """
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    return _run_op(root, ["push"], timeout=_NET_TIMEOUT)


def pull_ff(root: str | Path) -> dict[str, Any]:
    """快进拉取（`git pull --ff-only`）。

    非快进 git 自己报错、**不产生任何改动**（绝不自动产生合并）；有未解决的
    冲突文件时前置拒绝 —— 带着冲突去 pull 只会把状态搅得更乱。
    """
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    conflicts = unmerged(root)
    if conflicts:
        return {
            "available": True,
            "ok": False,
            "reason": "存在未解决的冲突文件，请先在终端解决后再拉取",
            "conflicts": conflicts,
            "output": "",
        }
    return _run_op(root, ["pull", "--ff-only"], timeout=_NET_TIMEOUT)


def branch_create(root: str | Path, name: str) -> dict[str, Any]:
    """新建并切换到分支：`git switch -c <name>`。"""
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    if not valid_refname(name):
        return {"available": True, "ok": False, "reason": f"分支名非法：{name!r}", "output": ""}
    return _run_op(root, ["switch", "-c", name])


def branch_switch(root: str | Path, name: str) -> dict[str, Any]:
    """切换分支：`git switch <name>`。"""
    root = Path(root)
    reason = _repo_reason(root)
    if reason:
        return _unavailable(reason)
    if not valid_refname(name):
        return {"available": True, "ok": False, "reason": f"分支名非法：{name!r}", "output": ""}
    return _run_op(root, ["switch", name])


__all__ = [
    "branch_create",
    "branch_switch",
    "branches",
    "commit",
    "diff",
    "discard",
    "is_repo",
    "log",
    "pull_ff",
    "push",
    "snapshot",
    "stage",
    "status",
    "unmerged",
    "unstage",
    "valid_refname",
]