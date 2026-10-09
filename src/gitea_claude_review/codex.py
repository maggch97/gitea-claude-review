"""Codex reviews with explicit auth and durable subscription refresh.

Only the CLI talks to OpenAI. The action never implements OAuth refresh itself.
Account auth is copied into an isolated home, then saved back to a repository
Secret or locked directory, even on a failed review. User configs, MCP servers
and old sessions are not copied. Use account auth on trusted private runners.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable

from .claude import ClaudeAnswer, Finding, Resolution, SEVERITIES


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


REVIEW_SCHEMA = _object({
    "summary": {"type": "string", "minLength": 1},
    "findings": {"type": "array", "items": _object({
        "path": {"type": "string", "minLength": 1},
        "line": {"type": "integer", "minimum": 1},
        "side": {"type": "string", "enum": ["new", "old"]},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "title": {"type": "string", "minLength": 1},
        "body": {"type": "string", "minLength": 1},
    })},
    "resolved": {"type": "array", "items": _object({
        "id": {"type": "integer", "minimum": 1}, "note": {"type": "string", "minLength": 1},
    })},
})

_ENV_ALLOW = {
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "TERM", "USER", "SHELL",
    "SYSTEMROOT", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "CODEX_CA_CERTIFICATE", "SSL_CERT_FILE",
}


def codex_env(home: Path, mode: str, credential: str, source: dict | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    env = {k: v for k, v in source.items() if k in _ENV_ALLOW and v}
    env["CODEX_HOME"] = str(home)
    # Shell profiles and user-level tools must not load the runner owner's secrets.
    env["HOME"] = str(home)
    if mode == "api-key":
        env["CODEX_API_KEY"] = credential
    elif mode == "access-token":
        env["CODEX_ACCESS_TOKEN"] = credential
    return env


def build_command(binary: str, model: str, effort: str, schema: Path, output: Path, *, yolo: bool = False) -> list[str]:
    # YOLO bypasses both controls; do not combine it with conflicting sandbox/approval flags.
    cmd = [binary, "exec", "--dangerously-bypass-approvals-and-sandbox"] if yolo else [
        binary, "--ask-for-approval", "never", "exec", "--sandbox", "read-only"]
    cmd += ["--ephemeral", "--ignore-user-config", "--ignore-rules", "--json",
           "--output-schema", str(schema), "--output-last-message", str(output),
           "-c", 'cli_auth_credentials_store="file"', "-c", 'web_search="disabled"',
           "-c", "features.multi_agent=false",
           "-c", 'shell_environment_policy.exclude=["CODEX_API_KEY","CODEX_ACCESS_TOKEN"]']
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    return cmd + ["-"]


def parse_answer(text: str) -> ClaudeAnswer:
    """Validate locally too: an incomplete review must never look successful."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Codex returned invalid review JSON") from None
    _validate(data, REVIEW_SCHEMA, "review")
    findings = []
    for item in data["findings"]:
        path = PurePosixPath(item["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in item["path"] or ":" in item["path"] or str(path) == ".":
            raise ValueError("Codex finding path must be repository-relative")
        findings.append(Finding(**item))
    return ClaudeAnswer(summary=data["summary"], findings=findings,
                        resolved=[Resolution(item["id"], item["note"]) for item in data["resolved"]])


def _validate(value, schema: dict, location: str) -> None:
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise ValueError(f"Codex {location} has missing or unexpected fields")
        for key, child in schema["properties"].items():
            _validate(value[key], child, f"{location}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"Codex {location} must be an array")
        for item in value:
            _validate(item, schema["items"], location + "[]")
    elif kind == "integer":
        if type(value) is not int or value < schema["minimum"]:
            raise ValueError(f"Codex {location} must be a positive integer")
    elif kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Codex {location} must be a non-empty string")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"Codex {location} has an invalid enum value")


def read_auth(text: str) -> dict:
    try:
        auth = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Codex auth.json is invalid JSON; reseed it with codex login") from None
    if not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt":
        raise ValueError("Codex auth.json must use auth_mode chatgpt")
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or any(not isinstance(tokens.get(k), str) or not tokens[k].strip()
                                          for k in ("access_token", "refresh_token", "id_token")):
        raise ValueError("Codex auth.json needs access, refresh and id tokens; reseed with codex login")
    return auth


def _atomic_write(path: Path, text: str) -> None:
    # Same-directory rename preserves the last valid cache after power loss.
    temporary = path.with_name(path.name + ".indask_atom_temp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def account_lock(home: Path, deadline: float):
    """OS lock survives crashes without stale-lock removal; never delete the lock file."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(home / ".review-auth.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            lock = lambda: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                lock()
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for Codex account auth lock") from None
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(fd)


def _execute(cmd: list[str], prompt: str, cwd: str, env: dict, timeout_s: float) -> None:
    if timeout_s <= 0:
        raise TimeoutError("Codex review timeout elapsed while waiting for authentication")
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", cwd=cwd, env=env,
                          start_new_session=os.name != "nt") as process:
        try:
            process.communicate(prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Kill agent-spawned shell commands too, before releasing the auth lock.
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise TimeoutError("Codex review exceeded timeout_minutes") from None
        if process.returncode:
            # Never echo the transcript/stderr: either can contain account tokens.
            raise RuntimeError(f"Codex exited with {process.returncode}; check CLI version, auth and model access")


def run_codex(prompt: str, binary: str, model: str, effort: str, cwd: str, timeout_s: int,
              *, auth_mode: str, account_home: str = "", auth_json: str = "",
              api_key: str = "", access_token: str = "", auth_storage: str = "gitea-secret",
              persist_auth: Callable[[str], None] | None = None, yolo: bool = False) -> ClaudeAnswer:
    if auth_mode not in ("chatgpt", "api-key", "access-token"):
        raise ValueError("codex_auth_mode must be chatgpt, api-key or access-token")
    if timeout_s <= 0:
        raise ValueError("timeout_minutes must be positive")
    if effort not in ("", "minimal", "low", "medium", "high", "xhigh"):
        raise ValueError("codex_effort must be minimal, low, medium, high or xhigh")
    if auth_mode == "chatgpt":
        if api_key or access_token:
            raise ValueError("ChatGPT auth cannot be mixed with API/access tokens")
        if auth_storage == "directory":
            if not account_home or persist_auth is not None:
                raise ValueError("Directory auth requires codex_home and no Secret write-back")
            home = Path(account_home)
            workspace = Path(cwd).resolve()
            if not home.is_absolute() or home.resolve() == workspace or workspace in home.resolve().parents:
                raise ValueError("codex_home must be an absolute persistent path outside the checkout")
        elif auth_storage == "gitea-secret":
            if account_home or not auth_json or persist_auth is None:
                raise ValueError("Secret auth requires codex_auth_json and Secret write-back, without codex_home")
        else:
            raise ValueError("codex_auth_storage must be gitea-secret or directory")
    elif account_home or auth_json or (auth_mode == "api-key" and (not api_key or access_token)) or (
            auth_mode == "access-token" and (not access_token or api_key)):
        raise ValueError("Provide only the credential matching codex_auth_mode; codex_home is for chatgpt only")
    deadline = time.monotonic() + timeout_s
    with tempfile.TemporaryDirectory(prefix="gcr-codex-") as folder:
        run_home = Path(folder)
        schema, output = run_home / "review-schema.json", run_home / "review-result.json"
        schema.write_text(json.dumps(REVIEW_SCHEMA), encoding="utf-8")
        cmd = build_command(binary, model, effort, schema, output, yolo=yolo)
        credential = api_key if auth_mode == "api-key" else access_token
        env = codex_env(run_home, auth_mode, credential)
        secrets = [credential] if credential else []
        if auth_mode == "chatgpt":
            with _account_source(auth_storage, account_home, auth_json, persist_auth, deadline) as (current, save):
                secrets.extend(read_auth(current)["tokens"].values())
                auth_path = run_home / "auth.json"
                _atomic_write(auth_path, current)
                try:
                    _execute(cmd, prompt, cwd, env, deadline - time.monotonic())
                finally:
                    refreshed = auth_path.read_text(encoding="utf-8")
                    secrets.extend(read_auth(refreshed)["tokens"].values())
                    if refreshed != current:
                        save(refreshed)
        else:
            _execute(cmd, prompt, cwd, env, deadline - time.monotonic())
        if not output.is_file():
            raise ValueError("Codex completed without a review result file")
        text = output.read_text(encoding="utf-8")
        if any(isinstance(secret, str) and secret and secret in text for secret in secrets):
            raise ValueError("Codex output contained credentials; refusing to publish it")
        return parse_answer(text)


@contextmanager
def _account_source(storage: str, directory: str, seed: str, persist: Callable[[str], None] | None,
                    deadline: float):
    if storage == "gitea-secret":
        # The workflow must serialize the entire job BEFORE its Secrets snapshot
        # is injected. A lock inside an ephemeral container cannot refresh that snapshot.
        read_auth(seed)
        yield seed, persist
    else:
        home = Path(directory)
        with account_lock(home, deadline):
            stored = home / "auth.json"
            if not stored.exists():
                if not seed:
                    raise ValueError("codex_home has no auth.json; seed it once with codex_auth_json or codex login")
                read_auth(seed)
                _atomic_write(stored, seed)
            current = stored.read_text(encoding="utf-8")
            read_auth(current)
            yield current, lambda refreshed: _atomic_write(stored, refreshed)
