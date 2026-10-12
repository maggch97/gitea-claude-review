"""Drain CLI streams live and log selected events without exposing credentials."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable


class ProgressLog:
    def __init__(self, provider: str, secrets: list[str] | None = None,
                 current_secrets: Callable[[], list[str]] | None = None):
        self.provider = provider
        self.secrets = list(secrets or [])
        self.current_secrets = current_secrets
        # The parent owns Gitea credentials too; do not limit redaction to the
        # smaller environment passed to the model subprocess.
        for key, value in os.environ.items():
            if value and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|AUTH_JSON", key):
                self.secrets.append(value)
                if key.endswith("AUTH_JSON"):
                    data = json.loads(value)
                    self.secrets.extend(v for v in data.get("tokens", {}).values() if isinstance(v, str))

    def redact(self, value: str) -> str:
        if self.current_secrets:
            self.secrets.extend(v for v in self.current_secrets() if isinstance(v, str))
            self.secrets = list(set(self.secrets))
        for secret in sorted(set(self.secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\b(?:sk-[\w-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b", "[REDACTED]", value)
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
        return value

    def write(self, label: str, value: object = "") -> None:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        # Redact before clipping, including tokens crossing the length boundary.
        text = self.redact(text)
        if len(text) > 6000:
            text = text[:6000] + " ... [truncated]"
        # Prefix every line: model/tool output must not become CI workflow commands.
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
        for line in text.splitlines() or [""]:
            print(f"[{self.provider}] {label}: {line}", flush=True)

    def event(self, event: dict) -> None:
        kind = event.get("type")
        if self.provider == "Claude":
            if kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        self.write("message", block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        self.write("tool", {"name": block.get("name"), "input": block.get("input")})
            elif kind == "user":
                # Never echo the user prompt; only tool results are progress.
                content = event.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            self.write("tool result", block.get("content", ""))
            elif kind == "system":
                self.write("status", event.get("subtype", "system"))
            elif kind == "result":
                self.write("result", event.get("result", ""))
        elif kind in {"thread.started", "turn.started", "turn.completed"}:
            self.write("status", kind)
        elif kind in {"turn.failed", "error"}:
            self.write("error", event.get("error", event.get("message", "CLI error")))
        elif kind in {"item.started", "item.completed"}:
            item = event.get("item", {})
            item_type = item.get("type")
            if item_type == "agent_message":
                self.write("message", item.get("text", ""))
            elif item_type == "command_execution":
                self.write(kind, {key: item[key] for key in ("command", "status", "exit_code", "aggregated_output") if key in item})
            elif item_type in {"mcp_tool_call", "web_search", "file_change", "todo_list"}:
                self.write(kind, item)
        # Reasoning/thinking blocks and unknown protocol payloads are not logs.


def run_stream(cmd: list[str], prompt: str, cwd: str, env: dict, timeout_s: float,
               log: ProgressLog, on_event: Callable[[dict], None]) -> int:
    """Consume stdout/stderr concurrently; enforce the deadline even with no output."""
    if timeout_s <= 0:
        raise TimeoutError(f"{log.provider} review exceeded timeout_minutes")
    deadline = time.monotonic() + timeout_s
    events: queue.Queue = queue.Queue()

    def drain(pipe, channel):
        try:
            for line in pipe:
                events.put((channel, line))
        except (OSError, UnicodeError) as error:
            events.put(("read_error", type(error).__name__))
        finally:
            events.put((channel, None))

    # A file-backed stdin avoids deadlock when a large prompt and verbose output
    # fill opposite pipes. It is private, temporary, and never part of artifacts.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as input_file:
        input_file.write(prompt)
        input_file.seek(0)
        with subprocess.Popen(cmd, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", cwd=cwd, env=env,
                              start_new_session=os.name != "nt") as process:
            readers = [threading.Thread(target=drain, args=(pipe, channel), daemon=True)
                       for pipe, channel in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
            for reader in readers:
                reader.start()
            try:
                closed = 0
                while closed < 2:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"{log.provider} review exceeded timeout_minutes")
                    try:
                        channel, line = events.get(timeout=min(remaining, 0.25))
                    except queue.Empty:
                        continue  # No event yet; the next iteration rechecks the deadline.
                    if line is None:
                        closed += 1
                    elif channel == "read_error":
                        raise RuntimeError(f"{log.provider} output could not be read: {line}")
                    elif channel == "stderr":
                        log.write("stderr", line.rstrip())
                    else:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            raise ValueError(f"{log.provider} emitted invalid JSON stream output") from None
                        if not isinstance(event, dict):
                            raise ValueError(f"{log.provider} stream event must be an object")
                        log.event(event)
                        on_event(event)
                try:
                    return process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise TimeoutError(f"{log.provider} review exceeded timeout_minutes") from None
            finally:
                # Also kill descendants retaining pipe handles when the parent
                # has exited; never release the auth lock while tools still run.
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # The entire process group has already exited.
                elif process.poll() is None:
                    process.kill()
                process.wait()
                for reader in readers:
                    reader.join(timeout=1)
