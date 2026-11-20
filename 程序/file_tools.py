"""AI 文件工具：工作区内可读写/删除/移动/运行脚本，工作区外只读。

安全规则（与《使用说明》保持一致）：
- 工作区可以包含多个文件夹，写入、删除、移动目标必须落在其中之一；
- 工作区外只允许读取，任何区外写入、删除、移动一律拒绝；
- 读取文件、列出目录、搜索文件、写入/修改、删除、移动、执行脚本都会弹窗确认；
- 敏感文件（.env、密钥、凭据等）与系统目录一律拒绝；
- 只处理文本文件，读取 64KB、写入 1MB 上限；删除前文件不超过 10MB 且必须备份；
- 删除只支持文件或空文件夹，不递归；移动不覆盖、不跨盘；
- 只执行工作区内已有的 .py/.bat/.cmd/.ps1 脚本，不接收参数，超时 120 秒；
- 覆盖/编辑/删除前自动备份，所有调用写入审计日志（不含正文）。
"""
from __future__ import annotations

import difflib
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

MAX_READ = 64 * 1024
MAX_WRITE = 1024 * 1024
MAX_DELETE = 10 * 1024 * 1024
MAX_RUN_OUTPUT = 64 * 1024
RUN_TIMEOUT = 120
MAX_LIST = 200
MAX_SEARCH_FILES = 2000
MAX_SEARCH_SIZE = 512 * 1024
MAX_SEARCH_HITS = 50
MAX_DIFF_LINES = 400
BACKUP_KEEP = 30

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonl", ".csv", ".tsv",
    ".log", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".css", ".scss", ".js", ".mjs", ".ts", ".tsx", ".jsx",
    ".vue", ".svelte", ".py", ".bat", ".cmd", ".ps1", ".sh", ".sql", ".tex",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".java", ".kt", ".go", ".rs", ".rb",
    ".php", ".lua", ".r", ".m", ".gradle", ".properties", ".gitignore",
    ".editorconfig", ".dockerignore",
}
TEXT_NAMES = {
    "dockerfile", "makefile", "readme", "license", "changelog", "cmakelists.txt",
}
SCRIPT_EXTS = {".py", ".bat", ".cmd", ".ps1"}
FORBIDDEN_PARTS = {
    ".git", "$recycle.bin", "system volume information", "appdata",
    "programdata", "program files", "program files (x86)", "windows",
}
FORBIDDEN_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx", ".keystore")
FORBIDDEN_NAMES = {
    ".netrc", "ntuser.dat", "credentials", "credentials.json",
    ".git-credentials", ".npmrc", ".pypirc", ".dockerconfigjson",
}
SENSITIVE_WORDS = ("secret", "credential", "apikey", "api_key", "access_key",
                   "private_key", "password")
RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_audit_lock = threading.Lock()


class ToolError(Exception):
    """参数或路径不合规，直接回给模型。"""


class ToolCancelled(ToolError):
    """用户拒绝了读、写或搜索操作。"""


def _forbidden_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value))
    for env in ("APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value))
    home = Path.home()
    roots.append(home / ".ssh")
    roots.append(home / ".aws")
    roots.append(home / ".azure")
    return roots


def validate_workspace(path: str | os.PathLike) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ToolError(f"工作区不是有效文件夹：{root}")
    if root.parent == root:
        raise ToolError("不能把磁盘根目录设为工作区")
    for forbidden in _forbidden_roots():
        try:
            forbidden = forbidden.resolve()
        except OSError:
            continue
        if root == forbidden or forbidden in root.parents:
            raise ToolError(f"该目录受保护，不能作为工作区：{root}")
    return root


def _check_forbidden(target: Path) -> None:
    parts = [p.lower() for p in target.parts]
    if any(part in FORBIDDEN_PARTS for part in parts):
        raise ToolError("该路径位于系统或凭据目录，已拒绝访问")
    name = target.name.lower()
    stem = target.stem.lower()
    if (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or name.startswith("id_rsa")
        or name.startswith("id_ed25519")
        or name in FORBIDDEN_NAMES
        or name.endswith(FORBIDDEN_SUFFIXES)
        or any(word in stem for word in SENSITIVE_WORDS)
    ):
        raise ToolError("该文件属于敏感配置，禁止读取或修改")
    for part in target.parts:
        stem = part.split(".")[0].lower()
        if stem in RESERVED_NAMES:
            raise ToolError("路径包含系统保留名称")


def _normalize_roots(roots) -> list[Path]:
    result: list[Path] = []
    for raw in roots or []:
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if path not in result:
            result.append(path)
    return result


def _containing_root(roots: list[Path], target: Path) -> Path | None:
    for root in roots:
        if target == root or root in target.parents:
            return root
    return None


def _resolve_target(roots: list[Path], raw: str) -> tuple[Path, bool]:
    """返回（绝对路径, 是否在工作区外）。区外只读，不做包含限制。"""
    text = (raw or ".").strip() or "."
    if len(text) > 500:
        raise ToolError("路径过长")
    if "\x00" in text:
        raise ToolError("路径包含非法字符")
    if text.startswith(("\\\\", "//")):
        raise ToolError("不允许使用网络路径")
    candidate = Path(text)
    absolute = candidate.is_absolute()
    if candidate.drive and not absolute:
        raise ToolError("请使用完整路径，例如 D:/项目/文件.txt")
    if absolute:
        # 防止 NTFS 数据流（例如 D:/a.txt:secret）与异常盘符
        rest = text[2:] if len(text) > 1 and text[1] == ":" else text
        if ":" in rest:
            raise ToolError("路径不能包含数据流冒号")
        try:
            target = candidate.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolError(f"无法解析路径：{exc}") from exc
    else:
        if ":" in text:
            raise ToolError("相对路径不能包含冒号")
        if not roots:
            raise ToolError("当前没有可用的工作区文件夹")
        try:
            target = (roots[0] / candidate).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolError(f"无法解析路径：{exc}") from exc
    inside = _containing_root(roots, target) is not None
    return target, not inside


def _require_text(path: Path) -> None:
    if path.suffix.lower() in TEXT_EXTS or path.name.lower() in TEXT_NAMES:
        return
    raise ToolError(f"只支持文本文件，已拒绝：{path.name}")


def _require_script(path: Path) -> None:
    if path.suffix.lower() not in SCRIPT_EXTS:
        raise ToolError("只允许执行 .py/.bat/.cmd/.ps1 脚本")
    if not path.is_file():
        raise ToolError("脚本文件不存在")


def _writable_root(roots: list[Path], target: Path) -> Path:
    root = _containing_root(roots, target)
    if root is None:
        raise ToolError("目标不在任何工作区文件夹内")
    if not root.is_dir():
        raise ToolError("工作区文件夹不存在，请重新编辑工作区")
    return root


def _reject_reparse(path: Path) -> None:
    """拒绝符号链接/目录联接，避免删除或移动实际落到工作区外。"""
    try:
        stat_result = os.lstat(path)
    except OSError as exc:
        raise ToolError(f"无法读取路径信息：{exc}") from exc
    attributes = getattr(stat_result, "st_file_attributes", 0)
    if path.is_symlink() or attributes & 0x400:
        raise ToolError("不支持符号链接或目录联接")


def _script_env() -> dict[str, str]:
    """脚本继承用户环境，但去掉密钥/令牌类变量，避免误读接口密钥。"""
    sensitive = ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "API_KEY", "APIKEY")
    env = {
        key: value for key, value in os.environ.items()
        if not any(word in key.upper() for word in sensitive)
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _decode_output(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "utf-8", "replace")


def _terminate_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, creationflags=0x08000000,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    try:
        proc.kill()
    except OSError:
        pass


def _label(roots: list[Path], target: Path) -> str:
    root = _containing_root(roots, target)
    if root is not None:
        try:
            rel = target.relative_to(root).as_posix()
            return rel or "."
        except ValueError:
            pass
    return target.as_posix()


def _ask_confirm(confirm, action: str, target: Path, detail: str,
                 *, kind: str, outside: bool) -> None:
    meta = {"kind": kind, "outside": outside}
    try:
        ok = confirm(action, target, detail, meta)
    except TypeError:
        # 兼容旧回调签名
        ok = confirm(action, target, detail)
    if not ok:
        verb = {
            "read": "读取", "write": "修改", "delete": "删除",
            "move": "移动", "execute": "执行",
        }.get(kind, "操作")
        raise ToolCancelled(f"用户拒绝了这次{verb}")


def _audit(audit_path: Path, action: str, path: str, detail: str,
           scope: str = "") -> None:
    line = (
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{action} path={path}"
        f"{f' ws={scope}' if scope else ''} {detail}\n"
    )
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with audit_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def _backup(root: Path, target: Path, backup_root: Path) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = backup_root / stamp
    serial = 1
    while folder.exists():
        folder = backup_root / f"{stamp}_{serial}"
        serial += 1
    dest = folder / target.relative_to(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dest)
    _prune_backups(backup_root)
    try:
        return str(folder.relative_to(backup_root.parent))
    except ValueError:
        return str(folder)


def _prune_backups(backup_root: Path) -> None:
    try:
        folders = sorted(
            (p for p in backup_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
        )
        for old in folders[:-BACKUP_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def make_diff(path_label: str, old: str | None, new: str, creating: bool) -> str:
    old_lines = (old or "").splitlines()
    new_lines = new.splitlines()
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="（新文件）" if creating else path_label,
            tofile=path_label,
            lineterm="",
            n=3,
        )
    )
    if not diff:
        return "（内容没有变化）"
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [
            f"… 共 {len(diff)} 行差异，仅显示前 {MAX_DIFF_LINES} 行"
        ]
    return "\n".join(diff)


def tool_schemas() -> list[dict]:
    def schema(name: str, description: str, props: dict, required: list[str]) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    path_prop = {
        "type": "string",
        "description": (
            "绝对路径（推荐，使用正斜杠，例如 D:/项目/说明.md），"
            "或相对第一个工作区文件夹的路径；工作区外只能读取"
        ),
    }
    return [
        schema(
            "list_dir",
            "列出指定目录的文件和子目录。每次调用都需要用户确认。",
            {"path": {**path_prop, "description": "目录绝对路径，默认工作区第一个文件夹"}},
            [],
        ),
        schema(
            "read_file",
            "读取一个文本文件（最多 64KB）。工作区内可读，工作区外只读；每次调用都需要用户确认。",
            {"path": path_prop},
            ["path"],
        ),
        schema(
            "search_files",
            "在指定目录的文本文件中搜索一段文字，返回 文件:行号 与所在行。每次调用都需要用户确认。",
            {
                "query": {"type": "string", "description": "要搜索的文字"},
                "path": {**path_prop, "description": "搜索起点目录，默认工作区第一个文件夹"},
            },
            ["query"],
        ),
        schema(
            "write_file",
            "新建或整体覆盖工作区内的文本文件。工作区外一律拒绝；覆盖前会备份，写入前需要用户确认。",
            {
                "path": path_prop,
                "content": {"type": "string", "description": "完整的新文件内容"},
            },
            ["path", "content"],
        ),
        schema(
            "edit_file",
            "在工作区内的文本文件中做一次精确替换。find 必须只出现一次；工作区外拒绝；修改前需要用户确认。",
            {
                "path": path_prop,
                "find": {"type": "string", "description": "要替换的原文，必须唯一匹配"},
                "replace": {"type": "string", "description": "替换后的文字"},
            },
            ["path", "find", "replace"],
        ),
        schema(
            "delete_file",
            "删除工作区内的文本文件或空文件夹。不递归删除；删除前文件会备份；每次调用都需要用户确认。",
            {"path": {**path_prop, "description": "要删除的文本文件或空文件夹绝对路径"}},
            ["path"],
        ),
        schema(
            "move_file",
            "移动或重命名工作区内的文件/文件夹。目标必须不存在、不能覆盖、不支持跨盘；每次调用都需要用户确认。",
            {
                "path": {**path_prop, "description": "原文件或文件夹的绝对路径"},
                "destination": {
                    **path_prop,
                    "description": "新路径（含新文件名）；目标必须不存在",
                },
            },
            ["path", "destination"],
        ),
        schema(
            "run_script",
            "运行工作区内已有的 .py/.bat/.cmd/.ps1 脚本。工作目录为脚本所在工作区，不接收参数，超时 120 秒；输出会返回给模型。每次调用都需要用户确认。",
            {"path": {**path_prop, "description": "脚本文件的绝对路径"}},
            ["path"],
        ),
    ]


def _check_keys(name: str, args: dict) -> None:
    allowed = {
        "list_dir": {"path"},
        "read_file": {"path"},
        "search_files": {"query", "path"},
        "write_file": {"path", "content"},
        "edit_file": {"path", "find", "replace"},
        "delete_file": {"path"},
        "move_file": {"path", "destination"},
        "run_script": {"path"},
    }[name]
    unknown = set(args) - allowed
    if unknown:
        raise ToolError(f"包含未知参数：{', '.join(sorted(unknown))}")


def _get_str(args: dict, key: str, required: bool = True, limit: int = 100000) -> str:
    value = args.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ToolError(f"参数 {key} 必须是字符串")
    if required and not value.strip() and key != "content":
        raise ToolError(f"缺少参数 {key}")
    if len(value) > limit:
        raise ToolError(f"参数 {key} 过长")
    return value


def _list_dir(roots: list[Path], args: dict, confirm) -> dict:
    target, outside = _resolve_target(
        roots, _get_str(args, "path", required=False) or "."
    )
    if not target.is_dir():
        raise ToolError("不是目录")
    _check_forbidden(target)
    where = "工作区外，只读" if outside else "工作区内"
    _ask_confirm(
        confirm, "列出目录", target,
        f"{where}。将把该目录的文件名、类型和大小发送给交大模型接口。",
        kind="read", outside=outside,
    )
    entries = []
    skipped = 0
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            _check_forbidden(item)
        except ToolError:
            skipped += 1
            continue
        if item.name.lower() in FORBIDDEN_PARTS:
            skipped += 1
            continue
        try:
            if item.is_dir():
                entries.append({
                    "name": item.name, "type": "dir",
                    "path": item.as_posix(),
                })
            else:
                entries.append({
                    "name": item.name, "type": "file",
                    "bytes": item.stat().st_size,
                    "path": item.as_posix(),
                })
        except OSError:
            continue
        if len(entries) >= MAX_LIST:
            break
    result = {
        "ok": True,
        "path": target.as_posix(),
        "scope": "outside" if outside else "workspace",
        "entries": entries,
        "summary": f"列出目录 {_label(roots, target)}（{len(entries)} 项）",
    }
    if skipped:
        result["protected_skipped"] = skipped
    if len(entries) >= MAX_LIST:
        result["truncated"] = True
    return result


def _read_file(roots: list[Path], args: dict, confirm) -> dict:
    target, outside = _resolve_target(roots, _get_str(args, "path"))
    _check_forbidden(target)
    _require_text(target)
    if not target.is_file():
        raise ToolError("文件不存在")
    try:
        size = target.stat().st_size
    except OSError:
        raise ToolError("无法读取文件大小")
    where = "工作区外，只读" if outside else "工作区内"
    _ask_confirm(
        confirm, "读取文件", target,
        f"{where}。文件 {size} 字节，读取后正文会发送给交大模型接口。",
        kind="read", outside=outside,
    )
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ToolError(f"读取失败：{exc}") from exc
    if b"\x00" in data[:4096]:
        raise ToolError("看起来是二进制文件，已拒绝")
    truncated = len(data) > MAX_READ
    text = data[:MAX_READ].decode("utf-8", "replace")
    return {
        "ok": True,
        "path": target.as_posix(),
        "scope": "outside" if outside else "workspace",
        "bytes": len(data),
        "truncated": truncated,
        "content": text,
        "summary": (
            f"读取 {_label(roots, target)}"
            f"（{len(data)} 字节{', 已截断' if truncated else ''}）"
        ),
    }


def _search_files(roots: list[Path], args: dict, confirm) -> dict:
    query = _get_str(args, "query")
    start, outside = _resolve_target(
        roots, _get_str(args, "path", required=False) or "."
    )
    if not start.is_dir():
        raise ToolError("搜索起点不是目录")
    _check_forbidden(start)
    where = "工作区外，只读" if outside else "工作区内"
    _ask_confirm(
        confirm, "搜索文件", start,
        f"{where}。搜索词：{query[:80]}\n命中结果（路径和所在行）会发送给交大模型接口。",
        kind="read", outside=outside,
    )
    needle = query.lower()
    hits: list[dict] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(start):
        base = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in FORBIDDEN_PARTS and not d.startswith(".git")
        ]
        for filename in filenames:
            target = base / filename
            try:
                _check_forbidden(target)
                resolved = target.resolve()
            except (OSError, RuntimeError, ValueError, ToolError):
                continue
            # 不跟随目录联接/符号链接跳出已确认的搜索范围
            if resolved != start and start not in resolved.parents:
                continue
            if not (
                resolved.suffix.lower() in TEXT_EXTS
                or resolved.name.lower() in TEXT_NAMES
            ):
                continue
            try:
                if resolved.stat().st_size > MAX_SEARCH_SIZE:
                    continue
            except OSError:
                continue
            scanned += 1
            if scanned > MAX_SEARCH_FILES:
                break
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in text[:1000]:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append({
                        "path": resolved.as_posix(),
                        "line": number,
                        "text": line.strip()[:200],
                    })
                    break
            if len(hits) >= MAX_SEARCH_HITS:
                break
        if len(hits) >= MAX_SEARCH_HITS or scanned > MAX_SEARCH_FILES:
            break
    return {
        "ok": True,
        "query": query,
        "path": start.as_posix(),
        "scope": "outside" if outside else "workspace",
        "hits": hits,
        "summary": f"搜索“{query[:40]}”命中 {len(hits)} 个文件",
    }


def _write_file(roots: list[Path], args: dict, backup_root: Path,
                audit_path: Path, confirm, scope: str) -> dict:
    target, outside = _resolve_target(roots, _get_str(args, "path"))
    if outside:
        raise ToolError("工作区外只允许读取，不能新建或修改文件")
    root = _containing_root(roots, target)
    if root is None:
        raise ToolError("目标不在任何工作区文件夹内")
    if not root.is_dir():
        raise ToolError("工作区文件夹不存在，请重新编辑工作区")
    _check_forbidden(target)
    _require_text(target)
    content = _get_str(args, "content", required=False, limit=MAX_WRITE)
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ToolError("内容超过 1MB 上限")
    if target.exists() and not target.is_file():
        raise ToolError("目标不是普通文件")
    old = None
    if target.exists():
        try:
            old_data = target.read_bytes()
        except OSError as exc:
            raise ToolError(f"读取原文件失败：{exc}") from exc
        if b"\x00" in old_data[:4096]:
            raise ToolError("目标看起来是二进制文件，已拒绝覆盖")
        old = old_data.decode("utf-8", "replace")
    label = _label(roots, target)
    diff = make_diff(label, old, content, creating=old is None)
    _ask_confirm(
        confirm, "写入文件", target, diff,
        kind="write", outside=False,
    )
    backup_note = ""
    if old is not None:
        backup_note = _backup(root, target, backup_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, data)
    _audit(
        audit_path, "WRITE", target.as_posix(),
        f"bytes={len(data)} backup={backup_note or '-'} allowed=1",
        scope,
    )
    return {
        "ok": True,
        "path": target.as_posix(),
        "scope": "workspace",
        "bytes": len(data),
        "backup": backup_note,
        "summary": (
            f"{'新建' if old is None else '覆盖'} {label}"
            f"（{len(data)} 字节）"
        ),
    }


def _edit_file(roots: list[Path], args: dict, backup_root: Path,
               audit_path: Path, confirm, scope: str) -> dict:
    target, outside = _resolve_target(roots, _get_str(args, "path"))
    if outside:
        raise ToolError("工作区外只允许读取，不能修改文件")
    root = _containing_root(roots, target)
    if root is None:
        raise ToolError("目标不在任何工作区文件夹内")
    if not root.is_dir():
        raise ToolError("工作区文件夹不存在，请重新编辑工作区")
    _check_forbidden(target)
    _require_text(target)
    if not target.is_file():
        raise ToolError("文件不存在")
    find = _get_str(args, "find", limit=MAX_WRITE)
    replace = _get_str(args, "replace", required=False, limit=MAX_WRITE)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ToolError(f"读取文件失败：{exc}") from exc
    if b"\x00" in data[:4096]:
        raise ToolError("目标看起来是二进制文件，已拒绝修改")
    old = data.decode("utf-8", "replace")
    count = old.count(find)
    if count == 0:
        raise ToolError("没有找到要替换的原文")
    if count > 1:
        raise ToolError(f"原文出现了 {count} 次，无法唯一匹配；请提供更长的片段")
    new = old.replace(find, replace, 1)
    if len(new.encode("utf-8")) > MAX_WRITE:
        raise ToolError("修改后文件超过 1MB 上限")
    label = _label(roots, target)
    diff = make_diff(label, old, new, creating=False)
    _ask_confirm(
        confirm, "修改文件", target, diff,
        kind="write", outside=False,
    )
    backup_note = _backup(root, target, backup_root)
    _atomic_write(target, new.encode("utf-8"))
    _audit(
        audit_path, "EDIT", target.as_posix(),
        f"bytes={len(new.encode('utf-8'))} backup={backup_note} allowed=1",
        scope,
    )
    return {
        "ok": True,
        "path": target.as_posix(),
        "scope": "workspace",
        "backup": backup_note,
        "summary": f"修改 {label}（替换 1 处）",
    }


def _delete_file(roots: list[Path], args: dict, backup_root: Path,
                 audit_path: Path, confirm, scope: str) -> dict:
    target, outside = _resolve_target(roots, _get_str(args, "path"))
    if outside:
        raise ToolError("工作区外只允许读取，不能删除")
    root = _writable_root(roots, target)
    if target == root:
        raise ToolError("不能删除工作区文件夹本身")
    _check_forbidden(target)
    if not target.exists():
        raise ToolError("目标不存在")
    _reject_reparse(target)

    is_file = target.is_file()
    if is_file:
        _require_text(target)
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ToolError(f"无法读取文件大小：{exc}") from exc
        if size > MAX_DELETE:
            raise ToolError("文件超过 10MB，无法先完整备份，已拒绝删除")
        action = "删除文件"
        detail = (
            f"路径：{target.as_posix()}\n"
            f"类型：文本文件\n"
            f"大小：{size} 字节\n\n"
            "删除前会把文件复制到 数据\\文件备份；删除后可用备份手动找回。\n"
            "这是不可逆操作，请确认路径无误。"
        )
    elif target.is_dir():
        try:
            if next(target.iterdir(), None) is not None:
                raise ToolError("文件夹不为空，不递归删除")
        except OSError as exc:
            raise ToolError(f"无法读取文件夹：{exc}") from exc
        action = "删除空文件夹"
        detail = (
            f"路径：{target.as_posix()}\n"
            "类型：空文件夹\n\n"
            "只会删除这个空文件夹本身，不递归删除任何内容；\n"
            "删除后目录本身不可自动恢复。"
        )
    else:
        raise ToolError("目标不存在，或不是普通文件/文件夹")

    _ask_confirm(
        confirm, action, target, detail,
        kind="delete", outside=False,
    )
    backup_note = ""
    if is_file:
        try:
            backup_note = _backup(root, target, backup_root)
        except OSError as exc:
            raise ToolError(f"备份失败，未删除：{exc}") from exc
        try:
            target.unlink()
        except OSError as exc:
            raise ToolError(f"删除失败：{exc}") from exc
    else:
        try:
            target.rmdir()
        except OSError as exc:
            raise ToolError(f"删除失败，文件夹可能已不为空：{exc}") from exc

    _audit(
        audit_path, "DELETE", target.as_posix(),
        f"type={'file' if is_file else 'empty_dir'} "
        f"backup={backup_note or '-'} allowed=1",
        scope,
    )
    return {
        "ok": True,
        "path": target.as_posix(),
        "scope": "workspace",
        "deleted": "file" if is_file else "empty_dir",
        "backup": backup_note,
        "summary": f"{'删除' if is_file else '删除空文件夹'} {_label(roots, target)}",
    }


def _move_file(roots: list[Path], args: dict, audit_path: Path,
               confirm, scope: str) -> dict:
    source, outside = _resolve_target(roots, _get_str(args, "path"))
    if outside:
        raise ToolError("工作区外只允许读取，不能移动或重命名")
    source_root = _writable_root(roots, source)
    if source == source_root:
        raise ToolError("不能移动工作区文件夹本身")
    _check_forbidden(source)
    if not source.exists():
        raise ToolError("原文件或文件夹不存在")
    _reject_reparse(source)

    destination, dest_outside = _resolve_target(
        roots, _get_str(args, "destination")
    )
    if dest_outside:
        raise ToolError("目标必须在工作区文件夹内")
    _writable_root(roots, destination)
    _check_forbidden(destination)
    if destination.exists():
        raise ToolError("目标已存在，不会覆盖")
    if source.is_dir() and (
        destination == source or source in destination.parents
    ):
        raise ToolError("不能把文件夹移动到它自己内部")
    if not destination.parent.is_dir():
        raise ToolError("目标所在文件夹不存在，请先创建")
    if source.drive.casefold() != destination.drive.casefold():
        raise ToolError("暂不支持跨盘移动，请先复制到目标盘再删除原文件")

    kind = "文件夹" if source.is_dir() else "文件"
    old_label = _label(roots, source)
    new_label = _label(roots, destination)
    detail = (
        f"原路径：{source.as_posix()}\n"
        f"新路径：{destination.as_posix()}\n"
        f"类型：{kind}\n\n"
        "目标必须不存在，不会覆盖已有文件；\n"
        "移动后如需撤销，可以把路径再移动回去。"
    )
    _ask_confirm(
        confirm, "移动或重命名", source, detail,
        kind="move", outside=False,
    )
    try:
        source.rename(destination)
    except OSError as exc:
        raise ToolError(f"移动失败：{exc}") from exc

    _audit(
        audit_path, "MOVE", source.as_posix(),
        f"to={destination.as_posix()} type={kind} allowed=1", scope,
    )
    return {
        "ok": True,
        "path": source.as_posix(),
        "destination": destination.as_posix(),
        "scope": "workspace",
        "summary": f"移动 {old_label} → {new_label}",
    }


def _run_script(roots: list[Path], args: dict, audit_path: Path,
                confirm, scope: str) -> dict:
    target, outside = _resolve_target(roots, _get_str(args, "path"))
    if outside:
        raise ToolError("工作区外只允许读取，不能执行脚本")
    root = _writable_root(roots, target)
    _check_forbidden(target)
    if not target.exists():
        raise ToolError("脚本文件不存在")
    _reject_reparse(target)
    _require_script(target)

    suffix = target.suffix.lower()
    if suffix == ".py":
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            candidate = python.with_name("python.exe")
            if candidate.is_file():
                python = candidate
        cmd = [str(python), str(target)]
    elif suffix in {".bat", ".cmd"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        cmd = [comspec, "/d", "/c", str(target)]
    else:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            raise ToolError("找不到 PowerShell，无法执行 .ps1 脚本")
        cmd = [
            powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(target),
        ]

    display = subprocess.list2cmdline(cmd)
    detail = (
        f"脚本：{target.as_posix()}\n"
        f"工作目录：{root.as_posix()}\n"
        f"启动命令：{display}\n\n"
        "超时 120 秒；输出最多 64KB，会发送给交大模型接口。\n"
        "风险：脚本以你的 Windows 用户权限运行，可以访问工作区外的文件、"
        "网络和系统资源。\n"
        "环境变量中的密钥/令牌类变量会被移除。请只运行你信任的脚本。"
    )
    _ask_confirm(
        confirm, "执行脚本", target, detail,
        kind="execute", outside=False,
    )

    flags = 0
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    started = time.monotonic()
    timed_out = False
    # 输出直接写入临时文件，避免脚本狂打印时全部堆进内存。
    with tempfile.TemporaryFile() as out_file:
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(root), env=_script_env(),
                stdin=subprocess.DEVNULL, stdout=out_file,
                stderr=subprocess.STDOUT, creationflags=flags,
            )
        except OSError as exc:
            raise ToolError(f"无法启动脚本：{exc}") from exc

        try:
            proc.wait(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        duration = time.monotonic() - started

        out_file.seek(0, os.SEEK_END)
        total = out_file.tell()
        out_file.seek(0)
        raw = out_file.read(MAX_RUN_OUTPUT + 1)
    truncated = total > MAX_RUN_OUTPUT
    output = _decode_output(raw[:MAX_RUN_OUTPUT])
    if truncated:
        output += "\n…（输出已截断，最多 64KB）"
    exit_code = proc.returncode
    if exit_code is None:
        exit_code = -1
    status = "超时" if timed_out else f"退出码 {exit_code}"
    _audit(
        audit_path, "RUN_SCRIPT", target.as_posix(),
        f"exit={exit_code} timed_out={int(timed_out)} "
        f"duration={duration:.1f}s allowed=1",
        scope,
    )
    return {
        "ok": True,
        "path": target.as_posix(),
        "scope": "workspace",
        "cwd": root.as_posix(),
        "command": display,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration": round(duration, 2),
        "truncated": truncated,
        "output": output,
        "summary": f"运行 {_label(roots, target)}（{status}，{duration:.1f}s）",
    }


def execute(
    name: str,
    args: dict,
    roots,
    *,
    backup_root: Path,
    audit_path: Path,
    confirm,
    scope: str = "",
) -> dict:
    """执行一次工具调用；confirm(action, path, detail, meta) -> bool 由界面提供。

    roots 是当前工作区的文件夹列表。读取允许越出工作区；
    写入、删除、移动和执行脚本只允许工作区内。
    """
    if name not in {
        "list_dir", "read_file", "search_files", "write_file", "edit_file",
        "delete_file", "move_file", "run_script",
    }:
        raise ToolError(f"未知工具：{name}")
    if not isinstance(args, dict):
        raise ToolError("参数必须是 JSON 对象")
    root_list = _normalize_roots(roots)
    raw_path = args.get("path")
    label = raw_path[:200] if isinstance(raw_path, str) else "."
    try:
        _check_keys(name, args)
        if name == "list_dir":
            result = _list_dir(root_list, args, confirm)
        elif name == "read_file":
            result = _read_file(root_list, args, confirm)
        elif name == "search_files":
            result = _search_files(root_list, args, confirm)
        elif name == "write_file":
            return _write_file(
                root_list, args, backup_root, audit_path, confirm, scope
            )
        elif name == "edit_file":
            return _edit_file(
                root_list, args, backup_root, audit_path, confirm, scope
            )
        elif name == "delete_file":
            return _delete_file(
                root_list, args, backup_root, audit_path, confirm, scope
            )
        elif name == "move_file":
            return _move_file(root_list, args, audit_path, confirm, scope)
        else:
            return _run_script(root_list, args, audit_path, confirm, scope)
    except ToolCancelled:
        _audit(audit_path, "DENY", label, f"tool={name} allowed=0", scope)
        raise
    except ToolError:
        _audit(audit_path, "REJECT", label, f"tool={name} allowed=0", scope)
        raise
    _audit(
        audit_path, name.upper(), result.get("path") or label,
        f"ok=1 scope={result.get('scope', '-')}", scope,
    )
    return result