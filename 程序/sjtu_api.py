#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上海交大“致远一号”模型 API 助手（OpenAI / Anthropic 双协议）。

典型用法：
  python sjtu_api.py models
  python sjtu_api.py chat -m deepseek-chat -p "用一句话解释量子纠缠"
  python sjtu_api.py chat -m deepseek-reasoner --file notes.txt -p "总结要点"
  python sjtu_api.py agent -m deepseek-chat --dir . "分析当前目录结构，只读不改"

密钥读取顺序：环境变量 SJTU_API_KEY > 同目录 .env 文件。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASE = "https://models.sjtu.edu.cn/api/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}
MODEL_ALIASES = {
    "chat": "deepseek-chat",
    "reasoner": "deepseek-reasoner",
    "deepseek": "deepseek-chat",
}

_ENV_CACHE: dict[str, str] | None = None


def env_file_values() -> dict[str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    values: dict[str, str] = {}
    path = HERE / ".env"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    _ENV_CACHE = values
    return values


def setting(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    return env_file_values().get(name, default)


def get_base() -> str:
    return (setting("SJTU_BASE_URL", DEFAULT_BASE) or DEFAULT_BASE).rstrip("/")


def load_key() -> str:
    key = (setting("SJTU_API_KEY") or "").strip()
    if not key:
        die("未找到 SJTU_API_KEY（请设置环境变量或在本目录 .env 中填写）", 2)
    return key


def die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


class ApiError(Exception):
    def __init__(self, status: int, body: str, headers: dict | None = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.headers = headers or {}


class TransportError(Exception):
    pass


def _headers(stream: bool) -> dict[str, str]:
    key = load_key()
    return {
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "sjtu-api-helper/1.0",
    }


class _Response:
    """同时持有连接和响应的薄包装，close 时把底层 socket 一起关掉。"""

    def __init__(self, conn, resp):
        self._conn = conn
        self._resp = resp

    def __getattr__(self, name):
        return getattr(self._resp, name)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __iter__(self):
        return iter(self._resp)

    def read(self, *args):
        return self._resp.read(*args)

    def close(self):
        try:
            self._resp.close()
        finally:
            self._conn.close()


def abort_connection(obj) -> None:
    """立即打断一个正在等待响应头或响应体的连接。"""
    conn = getattr(obj, "_conn", obj)
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        obj.close()
    except Exception:
        pass


def _open(path: str, payload: dict | None, timeout: float, stream: bool,
          on_open=None):
    """直接连接交大接口。

    不走系统/环境代理：从某些开发工具里启动本程序时会继承它们的本地代理，
    代理在流式请求上可能长时间不返回数据。交大接口在校园网内可直接访问。
    on_open 在发出请求前拿到连接，便于“停止”时立即关闭等待中的连接。
    """
    url = get_base() + path
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise TransportError(f"接口地址无效：{url}")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = conn_cls(parsed.hostname, parsed.port, timeout=timeout)
    try:
        if on_open is not None:
            # 先建立连接并交出句柄，这样“停止”能直接 shutdown socket。
            conn.connect()
            on_open(conn)
        conn.request(
            "POST" if data is not None else "GET",
            target,
            body=data,
            headers=_headers(stream),
        )
        resp = conn.getresponse()
        if resp.status >= 400:
            body = resp.read().decode("utf-8", "replace")
            headers = dict(resp.getheaders() or [])
            resp.close()
            conn.close()
            raise ApiError(resp.status, body, headers)
        return _Response(conn, resp)
    except ApiError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        try:
            conn.close()
        except Exception:
            pass
        raise TransportError(str(exc)) from None


def _retry_wait(exc: Exception, attempt: int) -> float:
    if isinstance(exc, ApiError):
        raw = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
        if raw:
            try:
                return min(60.0, max(1.0, float(raw)))
            except ValueError:
                pass
    return min(60.0, (5.0 * (2**attempt)) + random.uniform(0.0, 1.0))


def with_retries(fn, retries: int, label: str = "request"):
    attempt = 0
    while True:
        try:
            return fn()
        except (ApiError, TransportError) as exc:
            retriable = isinstance(exc, TransportError) or exc.status in RETRY_STATUS
            if not retriable or attempt >= retries:
                raise
            wait = _retry_wait(exc, attempt)
            print(
                f"[retry] {label} 失败（{exc}），{wait:.0f}s 后重试 ({attempt + 1}/{retries})",
                file=sys.stderr,
            )
            time.sleep(wait)
            attempt += 1


def request_json(path: str, payload: dict | None, timeout: float, retries: int, stream: bool):
    def attempt():
        response = _open(path, payload, timeout, stream)
        if stream:
            return response
        with response:
            raw = response.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(response.status, raw) from None

    return with_retries(attempt, retries, label=path)


def _protocol_path(protocol: str) -> str:
    return "/messages" if protocol == "anthropic" else "/chat/completions"


def extract_text(protocol: str, data: dict) -> str:
    if protocol == "anthropic":
        blocks = data.get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""


def extract_thinking(protocol: str, data: dict) -> str:
    if protocol == "anthropic":
        blocks = data.get("content") or []
        return "".join(
            b.get("thinking", "") for b in blocks if b.get("type") == "thinking"
        )
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("reasoning_content") or ""


def stop_reason(protocol: str, data: dict) -> str | None:
    if protocol == "anthropic":
        return data.get("stop_reason")
    choices = data.get("choices") or []
    return choices[0].get("finish_reason") if choices else None


def usage_line(protocol: str, data: dict) -> str | None:
    usage = data.get("usage") or {}
    if protocol == "anthropic":
        if "input_tokens" not in usage and "output_tokens" not in usage:
            return None
        return f"tokens: input={usage.get('input_tokens')} output={usage.get('output_tokens')}"
    if "prompt_tokens" not in usage and "completion_tokens" not in usage:
        return None
    return f"tokens: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}"


def stream_text(response, protocol: str, show_thinking: bool, print_events: bool = False) -> None:
    try:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if print_events:
                print(line)
                continue
            if not line.strip():
                if print_events:
                    print()
                continue
            if line.startswith("event:") or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if protocol == "anthropic":
                if obj.get("type") == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        sys.stdout.write(delta["text"])
                        sys.stdout.flush()
                    elif delta.get("type") == "thinking_delta" and show_thinking:
                        sys.stderr.write(delta.get("thinking", ""))
                        sys.stderr.flush()
                elif obj.get("type") == "error":
                    print(f"\n[stream error] {json.dumps(obj, ensure_ascii=False)}", file=sys.stderr)
            else:
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        sys.stdout.write(delta["content"])
                        sys.stdout.flush()
                    elif delta.get("reasoning_content") and show_thinking:
                        sys.stderr.write(delta["reasoning_content"])
                        sys.stderr.flush()
    finally:
        response.close()
    print()


def collect_input(args) -> str:
    parts: list[str] = []
    if args.prompt:
        parts.append(args.prompt)
    for file_name in args.file or []:
        path = Path(file_name)
        if not path.is_file():
            die(f"文件不存在: {file_name}", 2)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            die(f"读取文件失败 {file_name}: {exc}", 2)
        parts.append(f'<file path="{file_name}">\n{content}\n</file>')
    if args.stdin or (not parts and not sys.stdin.isatty()):
        data = sys.stdin.read()
        if data.strip():
            parts.append(data)
    return "\n\n".join(parts).strip()


def build_chat_payload(args, text: str) -> dict:
    system = args.system
    if args.system_file:
        system = Path(args.system_file).read_text(encoding="utf-8", errors="replace")
    payload: dict = {
        "model": args.model,
        "max_tokens": args.max_tokens,
    }
    if args.protocol == "anthropic":
        payload["messages"] = [{"role": "user", "content": text}]
        if system:
            payload["system"] = system
    else:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": text})
        payload["messages"] = messages
    if args.temperature is not None:
        payload["temperature"] = args.temperature
    if args.top_p is not None:
        payload["top_p"] = args.top_p
    if args.stream:
        payload["stream"] = True
    return payload


def normalize_model(name: str) -> str:
    return MODEL_ALIASES.get(name.lower(), name)


def cmd_models(args) -> int:
    data = request_json("/models", None, args.timeout, args.retries, stream=False)
    rows = data.get("data") or []
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"{'id':<24} owned_by")
    for row in rows:
        print(f"{row.get('id', ''):<24} {row.get('owned_by', '')}")
    return 0


def cmd_chat(args) -> int:
    args.model = normalize_model(args.model)
    if args.max_tokens is None:
        args.max_tokens = 4096 if ("reasoner" in args.model or args.model == "claw") else 2048
    text = collect_input(args)
    if not text:
        die("没有输入内容：请用 -p/--prompt、--file 或 stdin 提供", 2)
    payload = build_chat_payload(args, text)
    path = _protocol_path(args.protocol)
    if args.stream:
        response = request_json(path, payload, args.timeout, args.retries, stream=True)
        stream_text(response, args.protocol, args.show_thinking, args.events)
        return 0
    data = request_json(path, payload, args.timeout, args.retries, stream=False)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    output = extract_text(args.protocol, data)
    thinking = extract_thinking(args.protocol, data)
    print(output)
    if args.show_thinking and thinking:
        print(thinking, file=sys.stderr)
    if not args.quiet:
        line = usage_line(args.protocol, data)
        if line:
            print(f"[{args.model}] {line}", file=sys.stderr)
    if not output.strip():
        reason = stop_reason(args.protocol, data)
        print(
            f"[warn] 模型没有返回正文（stop_reason={reason}）。"
            "思考模型（reasoner/claw）请提高 --max-tokens，或改用 deepseek-chat。",
            file=sys.stderr,
        )
        if thinking:
            print(f"[warn] 思考内容：{thinking[:1000]}", file=sys.stderr)
        return 6
    if stop_reason(args.protocol, data) == "max_tokens":
        print("[warn] 输出被 max_tokens 截断，结果可能不完整。", file=sys.stderr)
    return 0


def cmd_raw(args) -> int:
    if args.body_file:
        raw = Path(args.body_file).read_text(encoding="utf-8")
    else:
        raw = args.body or ""
    if not raw.strip():
        die("需要 --body 或 --body-file 提供 JSON 请求体", 2)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"请求体不是合法 JSON: {exc}", 2)
    path = _protocol_path(args.protocol)
    if args.stream:
        response = request_json(path, payload, args.timeout, args.retries, stream=True)
        stream_text(response, args.protocol, args.show_thinking, args.events)
        return 0
    data = request_json(path, payload, args.timeout, args.retries, stream=False)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if not args.quiet:
        line = usage_line(args.protocol, data)
        if line:
            print(f"[raw] {line}", file=sys.stderr)
    return 0


def model_context_window(model: str) -> int:
    name = model.lower()
    if name.startswith("qwen"):
        return 262144
    if name.startswith("deepseek") or name == "claw":
        return 512000
    return 200000


def cmd_agent(args) -> int:
    model = normalize_model(args.model)
    prompt = args.prompt
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if not prompt or not prompt.strip():
        die("agent 模式需要任务描述：用 -p/--prompt 或 stdin", 2)

    key = load_key()
    base = get_base()
    anthropic_base = setting("SJTU_ANTHROPIC_BASE_URL") or (
        base[:-3] if base.endswith("/v1") else base
    )

    executable = shutil.which("claude")
    if not executable:
        die("未找到 claude CLI，无法启动交大子代理", 2)
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        command = ["cmd.exe", "/c", executable]
    else:
        command = [executable]

    workdir = Path(args.dir).resolve() if args.dir else Path.cwd()
    if not workdir.is_dir():
        die(f"--dir 不是目录: {workdir}", 2)

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ANTHROPIC_")}
    for name in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_BEDROCK_BASE_URL",
    ):
        env.pop(name, None)

    env.update(
        {
            "ANTHROPIC_BASE_URL": anthropic_base,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(
                args.context_tokens or model_context_window(model)
            ),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "API_TIMEOUT_MS": str(int(args.timeout * 1000)),
        }
    )

    command += [
        "-p",
        "--model",
        model,
        "--allowedTools",
        args.allowed_tools,
        "--disallowedTools",
        args.disallowed_tools,
        "--no-session-persistence",
    ]
    if args.append_system:
        command += ["--append-system-prompt", args.append_system]
    if args.output_format:
        command += ["--output-format", args.output_format]

    print(
        f"[sjtu-agent] model={model} tools={args.allowed_tools} dir={workdir}",
        file=sys.stderr,
    )
    capture = args.output_format == "json"
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(workdir),
            timeout=args.timeout,
            capture_output=capture,
        )
    except subprocess.TimeoutExpired:
        die(f"交大子代理超时（{args.timeout:.0f}s）", 4)

    if not capture:
        return completed.returncode

    stdout = (completed.stdout or "").strip()
    for line in (completed.stderr or "").splitlines():
        if "unrecognized_model" in line:
            continue
        print(line, file=sys.stderr)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        if stdout:
            print(stdout)
        return completed.returncode

    result = data.get("result") or ""
    if result:
        print(result)
    usage = data.get("usage") or {}
    print(
        f"[sjtu-agent] turns={data.get('num_turns')} "
        f"input={usage.get('input_tokens')} output={usage.get('output_tokens')} "
        f"duration={data.get('duration_ms')}ms",
        file=sys.stderr,
    )
    denials = data.get("permission_denials") or []
    if denials:
        print(
            f"[sjtu-agent] permission_denials={len(denials)}: "
            f"{json.dumps(denials, ensure_ascii=False)[:500]}",
            file=sys.stderr,
        )
    if data.get("is_error"):
        print(
            f"[sjtu-agent] error: subtype={data.get('subtype')} "
            f"api_error_status={data.get('api_error_status')}",
            file=sys.stderr,
        )
        return 5
    return completed.returncode


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=float(setting("SJTU_TIMEOUT", "300")))
    parser.add_argument("--retries", type=int, default=int(setting("SJTU_RETRIES", "3")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="上海交大“致远一号”模型 API 助手（OpenAI / Anthropic 双协议）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_models = sub.add_parser("models", help="列出可用模型")
    add_common(p_models)
    p_models.add_argument("--json", action="store_true")
    p_models.set_defaults(func=cmd_models)

    p_chat = sub.add_parser("chat", help="一次对话/文本任务")
    p_chat.add_argument("-m", "--model", default=setting("SJTU_DEFAULT_MODEL", "deepseek-chat"))
    p_chat.add_argument("-p", "--prompt")
    p_chat.add_argument("-f", "--file", action="append", default=[])
    p_chat.add_argument("--stdin", action="store_true")
    p_chat.add_argument("-s", "--system")
    p_chat.add_argument("--system-file")
    p_chat.add_argument("--max-tokens", type=int)
    p_chat.add_argument("--temperature", type=float)
    p_chat.add_argument("--top-p", type=float)
    p_chat.add_argument("--stream", action="store_true")
    p_chat.add_argument("--show-thinking", action="store_true")
    p_chat.add_argument("--events", action="store_true", help="打印原始 SSE 事件")
    p_chat.add_argument("--protocol", choices=["anthropic", "openai"], default="anthropic")
    p_chat.add_argument("--json", action="store_true")
    p_chat.add_argument("--quiet", action="store_true")
    add_common(p_chat)
    p_chat.set_defaults(func=cmd_chat)

    p_raw = sub.add_parser("raw", help="发送原始 JSON 请求（测试工具调用等）")
    p_raw.add_argument("--body")
    p_raw.add_argument("--body-file")
    p_raw.add_argument("--protocol", choices=["anthropic", "openai"], default="anthropic")
    p_raw.add_argument("--stream", action="store_true")
    p_raw.add_argument("--show-thinking", action="store_true")
    p_raw.add_argument("--events", action="store_true")
    p_raw.add_argument("--quiet", action="store_true")
    add_common(p_raw)
    p_raw.set_defaults(func=cmd_raw)

    p_agent = sub.add_parser(
        "agent", help="启动只读交大子代理（Claude Code 子代理 + 交大模型执行）"
    )
    p_agent.add_argument("-m", "--model", default=setting("SJTU_AGENT_MODEL", "deepseek-chat"))
    p_agent.add_argument("-p", "--prompt")
    p_agent.add_argument("--dir", default=".")
    p_agent.add_argument("--allowed-tools", default="Read,Grep,Glob")
    p_agent.add_argument(
        "--disallowed-tools",
        default="Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch,Task",
    )
    p_agent.add_argument("--append-system")
    p_agent.add_argument("--output-format", choices=["json", "text", "stream-json"], default="json")
    p_agent.add_argument("--context-tokens", type=int)
    p_agent.add_argument("--timeout", type=float, default=600.0)
    p_agent.set_defaults(func=cmd_agent)

    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ApiError as exc:
        detail = exc.body.strip()
        print(f"error: HTTP {exc.status}: {detail[:1500]}", file=sys.stderr)
        return 2 if exc.status in (401, 403) else 4
    except TransportError as exc:
        print(f"error: 网络请求失败: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
