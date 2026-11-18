#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenCANNBot multi-instance proxy center for Windows.

Features:
- Up to three isolated local proxy instances.
- Independent port, model, VK, PID, runtime home, and logs.
- Per-port forced model routing.
- Silent Windows startup and browser control center.
- Restart jobs with a polling progress page.

Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import ctypes
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

PROGRAM_DIR = Path(__file__).resolve().parent
WRAPPER_FILE = PROGRAM_DIR / "opencannbot_instance_proxy.py"
STYLE_FILE = PROGRAM_DIR / "proxy_center.css"
DASHBOARD_JS_FILE = PROGRAM_DIR / "dashboard.js"
JOB_JS_FILE = PROGRAM_DIR / "job.js"
EDIT_JS_FILE = PROGRAM_DIR / "edit.js"

APP_DIR = Path.home() / ".cannbot" / "proxy-center"
CONFIG_FILE = APP_DIR / "config.json"
PANEL_LOG_FILE = APP_DIR / "panel.log"
INSTANCES_DIR = APP_DIR / "instances"
OLD_CONFIG_FILE = Path.home() / ".cannbot" / "claude-panel.json"
OLD_VK_FILE = Path.home() / ".cannbot" / "vk"

MAX_INSTANCES = 3
DEFAULT_PORTS = (8766, 8767, 8768)
DEFAULT_MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("glm-5.3", "GLM 5.3"),
    ("glm-5.3-flash", "GLM 5.3 Flash"),
    # The gateway accepts these lowercase model IDs; titles are display-only.
    ("deepseek-v4-flash", "DeepSeek-V4-Flash"),
    ("deepseek-v4-pro", "DeepSeek-V4-Pro"),
)
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
MAX_MODELS = 100
MODEL_LABEL_MAX_LENGTH = 80

# Gateway model discovery. The OpenCANNBot proxy exchanges the VK for a JWT and
# exposes the authorized model catalog on the same host as the gateway.
MODELS_SYNC_TIMEOUT = 8
MODELS_SYNC_PAGE_SIZE = 100
MODELS_SYNC_MAX_PAGES = 20
MODELS_SYNC_MAX_AGE = 24 * 3600
MODELS_CACHE_FILE = APP_DIR / "models-cache.json"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
CSRF_TOKEN = secrets.token_urlsafe(32)
PANEL_PORT = 8788
CONFIG_LOCK = threading.RLock()
CONFIG_WRITE_LOCK = threading.RLock()
JOBS_LOCK = threading.RLock()
JOBS: dict[str, dict[str, Any]] = {}
MODELS_SYNC_LOCK = threading.RLock()
JWT_LOCK = threading.RLock()
JWT_CACHE: dict[str, dict[str, Any]] = {}
INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def is_windows() -> bool:
    return os.name == "nt"


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        APP_DIR.chmod(0o700)
    except OSError:
        pass


def panel_log(message: str) -> None:
    try:
        ensure_app_dirs()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with PANEL_LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    try:
        os.chmod(temporary, mode)
    except OSError:
        pass
    temporary.replace(path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def default_models() -> list[dict[str, Any]]:
    return [
        {"id": model_id, "label": label, "builtin": True}
        for model_id, label in DEFAULT_MODEL_OPTIONS
    ]


def normalize_models(raw: Any) -> list[dict[str, Any]]:
    """Return built-in models plus valid user-defined model routes."""
    models = default_models()
    seen = {item["id"] for item in models}
    if not isinstance(raw, list):
        return models

    for value in raw:
        if not isinstance(value, dict):
            continue
        model_id = str(value.get("id", value.get("model", ""))).strip()
        if not MODEL_ID_RE.fullmatch(model_id) or model_id in seen:
            continue
        label = str(value.get("label", value.get("name", model_id))).strip()
        if not label:
            label = model_id
        source = str(value.get("source", "custom")).strip().lower()
        if source not in ("custom", "gateway"):
            source = "custom"
        models.append({
            "id": model_id,
            "label": label[:MODEL_LABEL_MAX_LENGTH],
            "builtin": False,
            "source": source,
        })
        seen.add(model_id)
        if len(models) >= MAX_MODELS:
            break
    return models


def model_ids(models: list[dict[str, Any]]) -> set[str]:
    return {str(item["id"]) for item in models}


def default_config() -> dict[str, Any]:
    return {"version": 2, "repo_dir": "", "models": default_models(), "instances": []}


def new_instance_record(index: int, *, instance_id: Optional[str] = None) -> dict[str, Any]:
    safe_index = max(0, min(index, MAX_INSTANCES - 1))
    return {
        "id": instance_id or f"proxy-{uuid.uuid4().hex[:8]}",
        "name": f"代理实例 {safe_index + 1}",
        "description": "为应用提供独立模型路由",
        "port": DEFAULT_PORTS[safe_index],
        "model": "glm-5.3" if safe_index == 0 else "glm-5.3-flash",
        "log_level": "INFO",
        "keepalive_idle": 300,
        "socket_timeout": 30,
        "created_at": int(time.time()),
    }


def normalize_instance(
    raw: dict[str, Any],
    index: int,
    models: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    catalog = models or default_models()
    fallback = new_instance_record(index)
    instance = dict(fallback)
    instance.update({key: value for key, value in raw.items() if key in fallback})
    instance_id = str(instance.get("id", "")).lower().strip()
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        instance_id = f"proxy-{uuid.uuid4().hex[:8]}"
    instance["id"] = instance_id
    instance["name"] = str(instance.get("name", fallback["name"]))[:40].strip() or fallback["name"]
    instance["description"] = str(instance.get("description", ""))[:100].strip()
    try:
        instance["port"] = int(instance.get("port", fallback["port"]))
    except (TypeError, ValueError):
        instance["port"] = fallback["port"]
    if instance.get("model") not in model_ids(catalog):
        instance["model"] = fallback["model"]
    level = str(instance.get("log_level", "INFO")).upper()
    instance["log_level"] = level if level in LOG_LEVELS else "INFO"
    for key, default, low, high in (
        ("keepalive_idle", 300, 30, 3600),
        ("socket_timeout", 30, 5, 300),
    ):
        try:
            value = int(instance.get(key, default))
        except (TypeError, ValueError):
            value = default
        instance[key] = min(high, max(low, value))
    try:
        instance["created_at"] = int(instance.get("created_at", int(time.time())))
    except (TypeError, ValueError):
        instance["created_at"] = int(time.time())
    return instance


def migrate_legacy_config() -> Optional[dict[str, Any]]:
    legacy = read_json(OLD_CONFIG_FILE)
    if not legacy:
        return None
    repo_dir = str(legacy.get("repo_dir", "")).strip()
    first = new_instance_record(0, instance_id="proxy-default")
    first["name"] = "默认代理"
    first["description"] = "从旧版控制面板迁移"
    try:
        first["port"] = int(legacy.get("proxy_port", 8766))
    except (TypeError, ValueError):
        pass
    model = str(legacy.get("model", "glm-5.3"))
    first["model"] = model if model in model_ids(default_models()) else "glm-5.3"
    first["log_level"] = str(legacy.get("log_level", "INFO")).upper()
    first["keepalive_idle"] = int(legacy.get("keepalive_idle", 300))
    first["socket_timeout"] = int(legacy.get("socket_timeout", 30))
    config = {
        "version": 2,
        "repo_dir": repo_dir,
        "models": default_models(),
        "instances": [normalize_instance(first, 0, default_models())],
    }
    save_config(config)
    if OLD_VK_FILE.is_file():
        try:
            save_instance_vk(first["id"], OLD_VK_FILE.read_text(encoding="utf-8-sig").strip())
        except OSError:
            pass
    panel_log("Migrated legacy single-instance configuration")
    return config


def load_config() -> dict[str, Any]:
    ensure_app_dirs()
    with CONFIG_LOCK:
        if not CONFIG_FILE.exists():
            migrated = migrate_legacy_config()
            if migrated:
                return migrated
        raw = read_json(CONFIG_FILE) or default_config()
        config = default_config()
        config["repo_dir"] = str(raw.get("repo_dir", "")).strip()
        config["models"] = normalize_models(raw.get("models"))
        raw_instances = raw.get("instances", [])
        instances: list[dict[str, Any]] = []
        if isinstance(raw_instances, list):
            seen_ids: set[str] = set()
            for index, value in enumerate(raw_instances[:MAX_INSTANCES]):
                if not isinstance(value, dict):
                    continue
                instance = normalize_instance(value, index, config["models"])
                while instance["id"] in seen_ids:
                    instance["id"] = f"proxy-{uuid.uuid4().hex[:8]}"
                seen_ids.add(instance["id"])
                instances.append(instance)
        config["instances"] = instances
        return config


def save_config(config: dict[str, Any]) -> None:
    ensure_app_dirs()
    with CONFIG_LOCK:
        normalized = default_config()
        normalized["repo_dir"] = str(config.get("repo_dir", "")).strip()
        normalized["models"] = normalize_models(config.get("models"))
        normalized["instances"] = [
            normalize_instance(item, index, normalized["models"])
            for index, item in enumerate(config.get("instances", [])[:MAX_INSTANCES])
            if isinstance(item, dict)
        ]
        atomic_write(CONFIG_FILE, json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")


def find_instance(config: dict[str, Any], instance_id: str) -> Optional[dict[str, Any]]:
    for instance in config.get("instances", []):
        if instance.get("id") == instance_id:
            return instance
    return None


def instance_dir(instance_id: str) -> Path:
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError("实例 ID 无效")
    return INSTANCES_DIR / instance_id


def instance_vk_path(instance_id: str) -> Path:
    return instance_dir(instance_id) / "vk"


def instance_pid_path(instance_id: str) -> Path:
    return instance_dir(instance_id) / "pid.json"


def instance_log_path(instance_id: str) -> Path:
    return instance_dir(instance_id) / "proxy.log"


def instance_runtime_home(instance_id: str) -> Path:
    return instance_dir(instance_id) / "runtime-home"


def instance_launcher_ps1(instance_id: str) -> Path:
    return instance_dir(instance_id) / "claude-code.ps1"


def instance_launcher_cmd(instance_id: str) -> Path:
    return instance_dir(instance_id) / "claude-code.cmd"


def read_instance_vk(instance_id: str) -> str:
    try:
        return instance_vk_path(instance_id).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def save_instance_vk(instance_id: str, vk: str) -> None:
    if not vk:
        return
    atomic_write(instance_vk_path(instance_id), vk.strip() + "\n", 0o600)


def mask_vk(vk: str) -> str:
    if not vk:
        return "未配置"
    if len(vk) <= 12:
        return vk[:3] + "******"
    return vk[:7] + "…" + vk[-4:]


def repo_state(config: dict[str, Any]) -> dict[str, Any]:
    repo_text = str(config.get("repo_dir", "")).strip()
    repo = Path(repo_text).expanduser().resolve() if repo_text else Path(".").resolve()
    source = repo / "cannbot-claude-proxy.py"
    return {
        "repo": repo,
        "source": source,
        "ready": bool(repo_text) and repo.is_dir() and source.is_file() and WRAPPER_FILE.is_file(),
    }


def write_instance_launchers(instance: dict[str, Any]) -> None:
    instance_id = str(instance["id"])
    port = int(instance["port"])
    model = str(instance["model"])
    vk_path = instance_vk_path(instance_id)
    ps_vk = str(vk_path).replace("'", "''")
    model_ps = model.replace("'", "''")
    ps1 = (
        '$ErrorActionPreference = "Stop"\n'
        f'$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:{port}"\n'
        f"$env:ANTHROPIC_AUTH_TOKEN = (Get-Content -LiteralPath '{ps_vk}' -Raw).Trim()\n"
        f"$env:ANTHROPIC_MODEL = '{model_ps}'\n"
        '& claude @args\nexit $LASTEXITCODE\n'
    )
    cmd_vk = str(vk_path).replace("%", "%%")
    cmd_model = model.replace("%", "%%")
    cmd = (
        '@echo off\nsetlocal\n'
        f'set "ANTHROPIC_BASE_URL=http://127.0.0.1:{port}"\n'
        f'set /p ANTHROPIC_AUTH_TOKEN=<"{cmd_vk}"\n'
        f'set "ANTHROPIC_MODEL={cmd_model}"\n'
        'claude %*\nexit /b %ERRORLEVEL%\n'
    )
    atomic_write(instance_launcher_ps1(instance_id), ps1, 0o600)
    atomic_write(instance_launcher_cmd(instance_id), cmd, 0o600)


def write_all_launchers(config: dict[str, Any]) -> None:
    for instance in config.get("instances", []):
        write_instance_launchers(instance)


def load_pid(instance_id: str) -> Optional[dict[str, Any]]:
    data = read_json(instance_pid_path(instance_id))
    if data and isinstance(data.get("pid"), int):
        return data
    return None


def save_pid(instance: dict[str, Any], pid: int, source: Path) -> None:
    record = {
        "pid": int(pid),
        "port": int(instance["port"]),
        "model": str(instance["model"]),
        "source": str(source),
        "started_at": int(time.time()),
    }
    atomic_write(instance_pid_path(str(instance["id"])), json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def clear_pid(instance_id: str) -> None:
    try:
        instance_pid_path(instance_id).unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if is_windows():
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def health_check(instance: dict[str, Any], timeout: float = 1.5) -> dict[str, Any]:
    port = int(instance["port"])
    url = f"http://127.0.0.1:{port}/_health"
    try:
        request = Request(url, headers={"User-Agent": "OpenCANNBot-Proxy-Center/1.0"})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(65536).decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
            okay = bool(
                response.status == 200
                and isinstance(data, dict)
                and data.get("status") == "ok"
            )
            return {
                "ok": okay,
                "url": url,
                "data": data,
                "text": json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else raw,
            }
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except OSError:
            detail = ""
        return {"ok": False, "url": url, "data": None, "text": f"HTTP {exc.code}: {detail}"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "url": url, "data": None, "text": f"{type(exc).__name__}: {exc}"}


def instance_status(instance: dict[str, Any]) -> dict[str, Any]:
    instance_id = str(instance["id"])
    record = load_pid(instance_id)
    managed = bool(record and pid_alive(int(record["pid"])))
    if record and not managed:
        clear_pid(instance_id)
        record = None
    check = health_check(instance, timeout=0.65)
    healthy = bool(check["ok"])
    running = managed or healthy
    external = healthy and not managed
    health_data = check.get("data") or {}
    configured_model = health_data.get("configured_model") if isinstance(health_data, dict) else None
    drift: list[str] = []
    if record:
        if int(record.get("port", instance["port"])) != int(instance["port"]):
            drift.append("端口配置已变化")
        if str(record.get("model", instance["model"])) != str(instance["model"]):
            drift.append("模型配置已变化")
    if managed:
        message = f"运行中，PID {record['pid']}"
        if drift:
            message += "；" + "、".join(drift) + "，请重启"
        elif not healthy:
            message += "；健康检查失败"
    elif external:
        message = "端口上存在外部代理；代理中心不会强制停止它"
    else:
        message = "实例已停止"
    return {
        "running": running,
        "managed": managed,
        "external": external,
        "healthy": healthy,
        "pid": int(record["pid"]) if record else None,
        "message": message,
        "drift": bool(drift),
        "configured_model": configured_model,
        "health": check,
    }


def tail_file(path: Path, lines: int = 120) -> str:
    try:
        values = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(values[-lines:]) or "日志为空"
    except OSError:
        return "暂无日志"


def port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def validate_ports(config: dict[str, Any], *, ignore_id: Optional[str] = None, candidate_port: Optional[int] = None) -> None:
    used: dict[int, str] = {}
    for instance in config.get("instances", []):
        instance_id = str(instance["id"])
        if ignore_id and instance_id == ignore_id:
            continue
        port = int(instance["port"])
        used[port] = instance_id
    if candidate_port is not None and candidate_port in used:
        raise ValueError(f"端口 {candidate_port} 已被另一个实例使用")
    if candidate_port == PANEL_PORT:
        raise ValueError(f"端口 {candidate_port} 是代理中心页面端口，请换一个端口")


def proxy_environment(instance: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    runtime_home = instance_runtime_home(str(instance["id"]))
    runtime_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(runtime_home)
    env["USERPROFILE"] = str(runtime_home)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def start_instance(config: dict[str, Any], instance: dict[str, Any]) -> tuple[int, str]:
    state = repo_state(config)
    if not state["ready"]:
        return 2, f"项目目录无效或组件缺失：{state['repo']}"
    validate_ports(config, ignore_id=str(instance["id"]), candidate_port=int(instance["port"]))
    vk = read_instance_vk(str(instance["id"]))
    if not vk:
        return 2, "该实例尚未配置 VK"
    if not vk.startswith("vk-"):
        return 2, "该实例 VK 格式无效"
    current = instance_status(instance)
    if current["managed"]:
        return 0, current["message"]
    if current["running"]:
        return 3, "当前端口已有外部代理，无法重复启动"
    if not port_available(int(instance["port"])):
        return 3, f"端口 {instance['port']} 已被其他程序占用"

    directory = instance_dir(str(instance["id"]))
    directory.mkdir(parents=True, exist_ok=True)
    log_path = instance_log_path(str(instance["id"]))
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    command = [
        sys.executable,
        str(WRAPPER_FILE),
        "--source", str(state["source"]),
        "--model", str(instance["model"]),
        "--vk-file", str(instance_vk_path(str(instance["id"]))),
        "--host", "127.0.0.1",
        "--port", str(int(instance["port"])),
        "--log-level", str(instance["log_level"]),
        "--keepalive-idle", str(int(instance["keepalive_idle"])),
        "--socket-timeout", str(int(instance["socket_timeout"])),
    ]
    kwargs: dict[str, Any] = {
        "cwd": str(state["repo"]),
        "env": proxy_environment(instance),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if is_windows():
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        log_handle.close()
        return 1, f"启动失败：{exc}"
    finally:
        try:
            log_handle.close()
        except OSError:
            pass
    save_pid(instance, process.pid, state["source"])

    for _ in range(30):
        if process.poll() is not None:
            clear_pid(str(instance["id"]))
            return process.returncode or 1, "代理启动后立即退出：\n" + tail_file(log_path, 30)
        check = health_check(instance, timeout=0.5)
        data = check.get("data") or {}
        if check["ok"] and data.get("configured_model") == instance["model"]:
            return 0, f"实例已启动，PID {process.pid}，端口 {instance['port']}，模型 {instance['model']}"
        time.sleep(0.4)
    return 1, f"进程已启动，PID {process.pid}，但健康检查未在规定时间内通过"


def stop_instance(instance: dict[str, Any]) -> tuple[int, str]:
    instance_id = str(instance["id"])
    record = load_pid(instance_id)
    if not record:
        if health_check(instance, timeout=0.65)["ok"]:
            return 3, "检测到外部代理；为避免误杀，未执行停止"
        return 0, "实例已经停止"
    pid = int(record["pid"])
    if not pid_alive(pid):
        clear_pid(instance_id)
        return 0, "PID 记录已失效并清理"
    try:
        if is_windows():
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            if result.returncode != 0 and pid_alive(pid):
                return result.returncode, "停止失败：" + (result.stdout.strip() or "taskkill 返回错误")
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not pid_alive(pid):
                    break
                time.sleep(0.25)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"停止失败：{exc}"
    clear_pid(instance_id)
    for _ in range(12):
        if not health_check(instance, timeout=0.25)["ok"]:
            break
        time.sleep(0.2)
    return 0, f"实例已停止，原 PID {pid}"


def restart_instance(config: dict[str, Any], instance: dict[str, Any]) -> tuple[int, str]:
    current = instance_status(instance)
    if current["external"]:
        return 3, "实例端口由外部进程占用，无法自动重启"
    stop_code, stop_message = stop_instance(instance)
    if stop_code != 0:
        return stop_code, stop_message
    start_code, start_message = start_instance(config, instance)
    return start_code, stop_message + "\n" + start_message


def stop_all_instances(config: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    for instance in config.get("instances", []):
        code, message = stop_instance(instance)
        messages.append(f"{instance['name']}: [{code}] {message}")
    return messages

# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

def create_job(instance: dict[str, Any], action: str, title: str) -> str:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "instance_id": str(instance["id"]),
        "instance_name": str(instance["name"]),
        "action": action,
        "title": title,
        "message": "任务已创建，等待执行",
        "progress": 4,
        "started_at": time.time(),
        "done": False,
        "success": False,
        "error": "",
        "redirect_url": "/",
        "logs": [],
        "steps": [
            {"label": "保存实例配置", "detail": "端口、模型和认证配置已写入", "state": "done"},
            {"label": "停止旧进程", "detail": "等待处理", "state": "pending"},
            {"label": "启动新进程", "detail": "等待处理", "state": "pending"},
            {"label": "健康检查", "detail": "等待处理", "state": "pending"},
            {"label": "完成模型路由", "detail": "等待处理", "state": "pending"},
        ],
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    thread = threading.Thread(
        target=run_restart_job,
        args=(job_id,),
        name=f"proxy-job-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return job_id


def job_update(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(updates)


def job_log(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        stamp = time.strftime("%H:%M:%S")
        job.setdefault("logs", []).append(f"[{stamp}] {message}")
        job["logs"] = job["logs"][-80:]


def job_step(job_id: str, index: int, state: str, detail: str, progress: int) -> None:
    labels = {"pending": "等待中", "active": "进行中", "done": "已完成", "failed": "失败"}
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        steps = job.get("steps", [])
        if 0 <= index < len(steps):
            steps[index]["state"] = state
            steps[index]["state_label"] = labels.get(state, state)
            steps[index]["detail"] = detail
        job["progress"] = progress
        job["message"] = detail


def fail_job(job_id: str, step_index: int, error: str) -> None:
    job_step(job_id, step_index, "failed", error, 100)
    job_log(job_id, "ERROR: " + error)
    job_update(job_id, done=True, success=False, error=error, message=error, progress=100)


def run_restart_job(job_id: str) -> None:
    with JOBS_LOCK:
        initial = dict(JOBS.get(job_id, {}))
    instance_id = str(initial.get("instance_id", ""))
    try:
        config = load_config()
        instance = find_instance(config, instance_id)
        if not instance:
            fail_job(job_id, 1, "实例不存在或已被删除")
            return
        job_log(job_id, f"开始处理实例：{instance['name']}")
        job_log(job_id, f"目标端口：{instance['port']}，目标模型：{instance['model']}")

        current = instance_status(instance)
        job_step(job_id, 1, "active", "正在停止旧进程", 18)
        if current["external"]:
            fail_job(job_id, 1, "端口由外部进程占用，代理中心不能自动停止该进程")
            return
        if current["managed"]:
            code, message = stop_instance(instance)
            job_log(job_id, message)
            if code != 0:
                fail_job(job_id, 1, message)
                return
            job_step(job_id, 1, "done", "旧进程已停止", 34)
        else:
            job_log(job_id, "实例当前未运行，无需停止")
            job_step(job_id, 1, "done", "实例未运行，跳过停止", 34)

        job_step(job_id, 2, "active", "正在启动新进程", 46)
        config = load_config()
        instance = find_instance(config, instance_id)
        if not instance:
            fail_job(job_id, 2, "实例配置在任务期间被删除")
            return
        code, message = start_instance(config, instance)
        job_log(job_id, message)
        if code != 0:
            fail_job(job_id, 2, message)
            return
        job_step(job_id, 2, "done", "新进程已经启动", 68)

        job_step(job_id, 3, "active", "正在等待健康检查与模型路由确认", 76)
        success = False
        last_text = ""
        for attempt in range(20):
            check = health_check(instance, timeout=0.6)
            data = check.get("data") or {}
            last_text = check.get("text", "")
            if check["ok"] and data.get("configured_model") == instance["model"]:
                success = True
                break
            if attempt in (0, 4, 9, 14):
                job_log(job_id, f"健康检查等待中（{attempt + 1}/20）")
            time.sleep(0.45)
        if not success:
            fail_job(job_id, 3, "健康检查未通过或模型路由未生效：" + last_text[:300])
            return
        job_step(job_id, 3, "done", "健康检查通过，模型路由已确认", 90)
        job_log(job_id, f"模型路由已固定为 {instance['model']}")

        job_step(job_id, 4, "done", "实例已就绪，即将返回代理中心", 100)
        job_update(
            job_id,
            done=True,
            success=True,
            progress=100,
            message=f"{instance['name']} 已在端口 {instance['port']} 使用 {instance['model']}",
        )
    except Exception as exc:
        fail_job(job_id, 2, f"任务异常：{type(exc).__name__}: {exc}")
        panel_log(f"Job {job_id} failed: {type(exc).__name__}: {exc}")


def public_job(job_id: str) -> Optional[dict[str, Any]]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        output = json.loads(json.dumps(job, ensure_ascii=False))
    output["elapsed"] = max(0.0, time.time() - float(output.get("started_at", time.time())))
    for step in output.get("steps", []):
        if "state_label" not in step:
            step["state_label"] = {
                "pending": "等待中",
                "active": "进行中",
                "done": "已完成",
                "failed": "失败",
            }.get(step.get("state"), "等待中")
    return output


# ---------------------------------------------------------------------------
# Validation and configuration mutation
# ---------------------------------------------------------------------------

def parse_instance_form(form: dict[str, str], existing: Optional[dict[str, Any]], config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    index = len(config.get("instances", [])) if existing is None else config.get("instances", []).index(existing)
    instance = dict(existing or new_instance_record(index))
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    model = form.get("model", "").strip()
    log_level = form.get("log_level", "INFO").upper().strip()
    vk = form.get("vk", "").strip()
    try:
        port = int(form.get("port", ""))
        keepalive = int(form.get("keepalive_idle", "300"))
        socket_timeout = int(form.get("socket_timeout", "30"))
    except ValueError as exc:
        raise ValueError("端口和超时必须是整数") from exc
    if not name:
        raise ValueError("实例名称不能为空")
    if len(name) > 40:
        raise ValueError("实例名称不能超过 40 个字符")
    if len(description) > 100:
        raise ValueError("用途说明不能超过 100 个字符")
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    if model not in model_ids(model_catalog(config)):
        raise ValueError("请选择已配置的模型")
    if log_level not in LOG_LEVELS:
        raise ValueError("日志级别无效")
    if not 30 <= keepalive <= 3600:
        raise ValueError("最大空闲等待必须在 30–3600 秒之间")
    if not 5 <= socket_timeout <= 300:
        raise ValueError("Socket 超时必须在 5–300 秒之间")
    if vk and not vk.startswith("vk-"):
        raise ValueError("VK 必须以 vk- 开头")
    validate_ports(config, ignore_id=str(instance["id"]), candidate_port=port)
    instance.update({
        "name": name,
        "description": description,
        "port": port,
        "model": model,
        "log_level": log_level,
        "keepalive_idle": keepalive,
        "socket_timeout": socket_timeout,
    })
    return normalize_instance(instance, index, model_catalog(config)), vk


def update_instance_in_config(config: dict[str, Any], instance: dict[str, Any]) -> None:
    for index, current in enumerate(config.get("instances", [])):
        if current.get("id") == instance.get("id"):
            config["instances"][index] = instance
            return
    if len(config.get("instances", [])) >= MAX_INSTANCES:
        raise ValueError("最多只能创建 3 个代理实例")
    config.setdefault("instances", []).append(instance)


def next_available_port(config: dict[str, Any]) -> int:
    used = {int(item["port"]) for item in config.get("instances", [])}
    for port in DEFAULT_PORTS:
        if port not in used and port != PANEL_PORT:
            return port
    candidate = 8766
    while candidate in used or candidate == PANEL_PORT:
        candidate += 1
    return candidate


def delete_instance(config: dict[str, Any], instance: dict[str, Any]) -> tuple[int, str]:
    current = instance_status(instance)
    if current["external"]:
        return 3, "端口存在外部代理，无法安全删除实例"
    if current["managed"]:
        code, message = stop_instance(instance)
        if code != 0:
            return code, message
    config["instances"] = [item for item in config.get("instances", []) if item.get("id") != instance.get("id")]
    save_config(config)
    try:
        shutil.rmtree(instance_dir(str(instance["id"])))
    except OSError:
        pass
    return 0, "实例已删除"


def find_model(config: dict[str, Any], model_id: str) -> Optional[dict[str, Any]]:
    for item in model_catalog(config):
        if item.get("id") == model_id:
            return item
    return None


def upsert_model(config: dict[str, Any], model_id: str, label: str) -> str:
    model_id = model_id.strip()
    label = label.strip() or model_id
    if not MODEL_ID_RE.fullmatch(model_id):
        raise ValueError("模型 ID 只能包含字母、数字、点、下划线、冒号、斜线和连字符")
    if len(label) > MODEL_LABEL_MAX_LENGTH:
        raise ValueError(f"模型显示名称不能超过 {MODEL_LABEL_MAX_LENGTH} 个字符")

    catalog = model_catalog(config)
    existing = next((item for item in catalog if item["id"] == model_id), None)
    if existing:
        if existing.get("builtin"):
            raise ValueError("内置模型不能修改")
        existing["label"] = label
        config["models"] = catalog
        return f"模型 {model_id} 已更新"
    if len(catalog) >= MAX_MODELS:
        raise ValueError(f"最多只能配置 {MAX_MODELS} 个模型")
    catalog.append({"id": model_id, "label": label, "builtin": False})
    config["models"] = catalog
    return f"模型 {model_id} 已添加"


def remove_model(config: dict[str, Any], model_id: str) -> str:
    catalog = model_catalog(config)
    existing = next((item for item in catalog if item["id"] == model_id), None)
    if not existing:
        raise ValueError("模型不存在")
    if existing.get("builtin"):
        raise ValueError("内置模型不能删除")
    using = [
        str(item["name"])
        for item in config.get("instances", [])
        if item.get("model") == model_id
    ]
    if using:
        raise ValueError(f"模型正在被实例使用：{'、'.join(using)}；请先切换实例模型")
    config["models"] = [item for item in catalog if item["id"] != model_id]
    return f"模型 {model_id} 已删除"


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def model_catalog(config: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    if config and isinstance(config.get("models"), list):
        return normalize_models(config["models"])
    return default_models()


def model_label(model: str, config: Optional[dict[str, Any]] = None) -> str:
    for item in model_catalog(config):
        if item["id"] == model:
            return str(item["label"])
    return model


def model_options(current: str, config: Optional[dict[str, Any]] = None) -> str:
    return "".join(
        f'<option value="{esc(item["id"])}" {"selected" if item["id"] == current else ""}>'
        f'{esc(item["label"])} · {esc(item["id"])}</option>'
        for item in model_catalog(config)
    )


# ---------------------------------------------------------------------------
# Gateway model discovery
# ---------------------------------------------------------------------------

def get_mac_address() -> str:
    """Return a stable MAC address for the VK->JWT exchange, or a placeholder."""
    try:
        mac_int = uuid.getnode()
        if (mac_int >> 40) % 2 == 0:  # not a random address
            return ":".join(
                f"{(mac_int >> offset) & 0xff:02x}"
                for offset in (40, 32, 24, 16, 8, 0)
            )
    except Exception:
        pass
    return "00:00:00:00:00:00"


def gateway_endpoints(config: dict[str, Any]) -> tuple[str, str]:
    """Read the model catalog and authentication endpoints from OpenCANNBot."""
    repo = repo_state(config)
    if not repo["ready"]:
        raise ValueError("请在全局设置中配置有效的 OpenCANNBot 仓库，再同步模型")

    try:
        text = repo["source"].read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError("无法读取 OpenCANNBot 代理源码") from exc

    gateway_match = re.search(r"""^GATEWAY_URL\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    if not gateway_match:
        raise ValueError("OpenCANNBot 代理源码缺少 GATEWAY_URL，无法同步模型")
    try:
        gateway = urlparse(gateway_match.group(1).strip())
    except ValueError as exc:
        raise ValueError("OpenCANNBot 代理源码中的 GATEWAY_URL 无效") from exc
    if gateway.scheme not in ("http", "https") or not gateway.hostname or gateway.username is not None:
        raise ValueError("OpenCANNBot 代理源码中的 GATEWAY_URL 无效")

    origin = f"{gateway.scheme}://{gateway.netloc}"
    auth_url = origin + "/cannbot/api/auth/authenticate"
    auth_match = re.search(r"""^AUTH_URL\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    if auth_match:
        candidate = auth_match.group(1).strip()
        try:
            auth = urlparse(candidate)
        except ValueError:
            auth = None
        if auth and (auth.scheme, auth.netloc) == (gateway.scheme, gateway.netloc):
            auth_url = candidate
        else:
            panel_log("Ignoring AUTH_URL with a different or invalid origin")
    return auth_url, origin + "/cannbot/api/models/list"


def _jwt_cache_key(auth_url: str, vk: str) -> str:
    return auth_url + "\n" + vk


def exchange_vk_for_jwt(
    vk: str,
    auth_url: str,
    timeout: float = MODELS_SYNC_TIMEOUT,
    force: bool = False,
) -> tuple[Optional[str], str]:
    """Exchange a VK for a short-lived JWT (cached per auth host + VK)."""
    now = time.time()
    cache_key = _jwt_cache_key(auth_url, vk)
    with JWT_LOCK:
        cached = JWT_CACHE.get(cache_key)
        if not force and cached and float(cached.get("expires", 0)) > now + 60:
            return str(cached.get("token", "")), ""

    body = json.dumps({"type": "cli", "mac": get_mac_address()}).encode("utf-8")
    request = Request(auth_url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("x-api-vkey", vk)
    request.add_header("User-Agent", "OpenCANNBot-Proxy-Center/1.0")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            detail = ""
        return None, f"认证请求失败：HTTP {exc.code} {detail}".strip()
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"认证请求失败：{type(exc).__name__}: {exc}"

    if not isinstance(payload, dict):
        return None, "认证响应格式无法识别"
    token = payload.get("accessToken") or payload.get("access_token")
    if not token:
        return None, "认证响应缺少 accessToken"
    try:
        expires_in = int(payload.get("expiresIn") or payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    expires = now + max(120, expires_in) - 60
    with JWT_LOCK:
        JWT_CACHE[cache_key] = {"token": str(token), "expires": expires}
    return str(token), ""


def fetch_gateway_models(models_url: str, token: str, timeout: float = MODELS_SYNC_TIMEOUT) -> tuple[Optional[list[dict[str, Any]]], str]:
    """Fetch every enabled model from the gateway model list API."""
    discovered: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while page <= MODELS_SYNC_MAX_PAGES:
        url = f"{models_url}?page={page}&size={MODELS_SYNC_PAGE_SIZE}"
        request = Request(url)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "OpenCANNBot-Proxy-Center/1.0")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except OSError:
                detail = ""
            return None, f"模型列表请求失败：HTTP {exc.code} {detail}".strip()
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            return None, f"模型列表请求失败：{type(exc).__name__}: {exc}"

        if isinstance(payload, dict):
            items = payload.get("models")
            pages = payload.get("totalPages") or payload.get("total_pages") or 1
        elif isinstance(payload, list):
            items = payload
            pages = 1
        else:
            return None, "模型列表响应格式无法识别"
        if not isinstance(items, list):
            return None, "模型列表响应缺少 models 字段"

        for item in items:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status is not None and str(status).strip().lower() not in ("1", "true", "active"):
                continue
            model_id = str(item.get("model") or item.get("id") or "").strip()
            if not MODEL_ID_RE.fullmatch(model_id) or model_id in seen:
                continue
            label = str(item.get("title") or item.get("name") or model_id).strip()
            entry: dict[str, Any] = {
                "id": model_id,
                "label": (label or model_id)[:MODEL_LABEL_MAX_LENGTH],
            }
            for cache_key, source_key in (("context_length", "contextLength"), ("max_tokens", "maxTokens")):
                value = item.get(source_key)
                if isinstance(value, int):
                    entry[cache_key] = value
            discovered.append(entry)
            seen.add(model_id)

        try:
            total_pages = int(pages)
        except (TypeError, ValueError):
            total_pages = 1
        if total_pages <= 1 or page >= total_pages:
            break
        page += 1

    if not discovered:
        return None, "网关没有返回可用模型（当前 VK 可能未授权任何模型）"
    return discovered, ""


def configured_vk_candidates(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Return distinct instance VKs plus the legacy global VK as a fallback."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for instance in config.get("instances", []):
        vk = read_instance_vk(str(instance["id"]))
        if vk and vk not in seen:
            candidates.append((vk, f"实例 {instance['name']}"))
            seen.add(vk)
    try:
        global_vk = OLD_VK_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        global_vk = ""
    if global_vk and global_vk not in seen:
        candidates.append((global_vk, "全局 VK"))
    return candidates


def sync_models_from_gateway(config: dict[str, Any]) -> tuple[list[dict[str, Any]], str, bool]:
    """Query every configured VK and return the union of their gateway catalogs.

    The third value is True only when all VKs were queried successfully; callers
    must not remove catalog entries when the result is incomplete.
    """
    candidates = configured_vk_candidates(config)
    if not candidates:
        raise ValueError("未找到可用的 VK，请先在实例配置中填写 VK")
    auth_url, models_url = gateway_endpoints(config)
    failures: list[str] = []
    union: dict[str, dict[str, Any]] = {}
    succeeded: list[str] = []
    for vk, source_name in candidates:
        token, error = exchange_vk_for_jwt(vk, auth_url)
        models: Optional[list[dict[str, Any]]] = None
        if token:
            models, error = fetch_gateway_models(models_url, token)
            if models is None and ("HTTP 401" in error or "HTTP 403" in error):
                # The cached JWT was rejected; refresh it once and retry.
                token, refresh_error = exchange_vk_for_jwt(vk, auth_url, force=True)
                if token:
                    models, error = fetch_gateway_models(models_url, token)
                else:
                    error = refresh_error or error
        if models:
            succeeded.append(source_name)
            for item in models:
                union.setdefault(str(item["id"]), item)
        else:
            failures.append(f"{source_name}：{error}")
    if not union:
        detail = "；".join(failures) if failures else "网关没有返回可用模型"
        raise ValueError("从网关获取模型列表失败：" + detail)
    if failures:
        panel_log("Model sync partial failures: " + "；".join(failures))
    if len(succeeded) <= 2:
        source = "、".join(succeeded)
    else:
        source = f"{len(succeeded)} 个 VK"
    return list(union.values()), source, not failures


def read_models_cache() -> dict[str, Any]:
    value = read_json(MODELS_CACHE_FILE)
    return value if isinstance(value, dict) else {}


def format_timestamp(value: Any) -> str:
    try:
        stamp = int(value)
        if stamp <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _write_models_cache_payload(payload: dict[str, Any]) -> None:
    try:
        atomic_write(
            MODELS_CACHE_FILE,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError as exc:
        panel_log(f"Failed to write model cache: {type(exc).__name__}: {exc}")


def write_models_cache(discovered: list[dict[str, Any]], source: str, *, complete: bool = True) -> None:
    now = int(time.time())
    _write_models_cache_payload({
        "synced_at": now,
        "last_attempt": now,
        "last_error": "",
        "source": source,
        "complete": bool(complete),
        "models": discovered,
    })


def record_sync_failure(message: str) -> None:
    cache = read_models_cache()
    cache["last_attempt"] = int(time.time())
    cache["last_error"] = str(message)[:500]
    if not isinstance(cache.get("models"), list):
        cache["models"] = []
    _write_models_cache_payload(cache)


def merge_discovered_models(
    config: dict[str, Any],
    discovered: list[dict[str, Any]],
    *,
    prune_missing: bool = True,
) -> dict[str, int]:
    """Merge gateway models into the catalog, keeping built-in and manual entries.

    When *prune_missing* is True (a complete sync) any model the gateway does not
    provide is removed unless an instance still uses it. Manual entries that the
    gateway does provide are kept with their custom label.
    """
    catalog = model_catalog(config)
    discovered_map = {str(item["id"]): item for item in discovered}
    used = {str(instance.get("model", "")) for instance in config.get("instances", [])}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    added = updated = removed = kept_stale = 0

    for item in catalog:
        model_id = str(item["id"])
        if item.get("builtin"):
            merged.append(item)
            seen.add(model_id)
            continue
        source = str(item.get("source", "custom")).lower()
        if model_id in discovered_map:
            if source == "gateway":
                entry = dict(item)
                label = str(discovered_map[model_id].get("label", "")).strip()
                if label and label != entry.get("label"):
                    entry["label"] = label[:MODEL_LABEL_MAX_LENGTH]
                    updated += 1
                merged.append(entry)
            else:
                merged.append(item)
            seen.add(model_id)
            continue
        if model_id in used or not prune_missing:
            merged.append(item)
            seen.add(model_id)
            kept_stale += 1
        else:
            removed += 1

    for item in discovered:
        model_id = str(item["id"])
        if model_id in seen or len(merged) >= MAX_MODELS:
            continue
        merged.append({
            "id": model_id,
            "label": str(item.get("label") or model_id)[:MODEL_LABEL_MAX_LENGTH],
            "builtin": False,
            "source": "gateway",
        })
        seen.add(model_id)
        added += 1

    config["models"] = merged
    return {"added": added, "updated": updated, "removed": removed, "kept": kept_stale, "total": len(merged)}


def gateway_unlisted_models(config: dict[str, Any], discovered_ids: set[str]) -> list[str]:
    """Manual models that are absent from the gateway model list."""
    return [
        str(item["id"])
        for item in model_catalog(config)
        if not item.get("builtin")
        and str(item.get("source", "custom")).lower() == "custom"
        and str(item["id"]) not in discovered_ids
    ]


def sync_models_into_config() -> str:
    """Fetch the gateway catalog, merge it into the config, and return a summary."""
    with MODELS_SYNC_LOCK:
        # Network calls happen outside config locks so status polling stays responsive.
        snapshot = load_config()
        try:
            discovered, source, complete = sync_models_from_gateway(snapshot)
        except ValueError as exc:
            record_sync_failure(str(exc))
            raise
        with CONFIG_WRITE_LOCK, CONFIG_LOCK:
            config = load_config()
            stats = merge_discovered_models(config, discovered, prune_missing=complete)
            save_config(config)
            write_models_cache(discovered, source, complete=complete)
    message = (
        f"已从网关同步 {len(discovered)} 个模型（来源：{source}）："
        f"新增 {stats['added']} 个、更新 {stats['updated']} 个"
    )
    if not complete:
        message += "；部分 VK 同步失败，本次未移除网关未返回的模型"
    if stats["removed"]:
        message += f"、移除 {stats['removed']} 个网关未提供的模型"
    if stats["kept"]:
        if complete:
            message += f"；{stats['kept']} 个网关未提供的模型仍被实例使用，暂未移除"
        else:
            message += f"；保留 {stats['kept']} 个网关未返回的旧模型"
    return message


def prune_unlisted_models(model_ids: Optional[set[str]] = None) -> str:
    """Remove manual models that the gateway no longer lists (except in-use ones).

    When *model_ids* is given, only that confirmed set is removed; this keeps the
    POST bound to the IDs shown on the confirmation page.
    """
    with MODELS_SYNC_LOCK:
        cache = read_models_cache()
        cached_models = cache.get("models") if isinstance(cache.get("models"), list) else []
        discovered_ids = {str(item.get("id", "")) for item in cached_models if isinstance(item, dict)}
        if not discovered_ids:
            raise ValueError("请先从网关同步模型列表，再清理未列出的模型")
        if cache.get("complete") is False:
            raise ValueError("上次同步未覆盖全部 VK，请重新同步后再清理")
        try:
            synced_at = int(cache.get("synced_at") or 0)
        except (TypeError, ValueError):
            synced_at = 0
        if synced_at and time.time() - synced_at > MODELS_SYNC_MAX_AGE:
            panel_log("Model cache is older than 24h; pruning may remove still-valid models")
        removed: list[str] = []
        blocked: list[str] = []
        with CONFIG_WRITE_LOCK, CONFIG_LOCK:
            config = load_config()
            catalog = model_catalog(config)
            used = {str(instance.get("model", "")) for instance in config.get("instances", [])}
            kept: list[dict[str, Any]] = []
            for item in catalog:
                model_id = str(item["id"])
                if item.get("builtin") or str(item.get("source", "custom")).lower() == "gateway":
                    kept.append(item)
                elif model_id in discovered_ids:
                    kept.append(item)
                elif model_id in used:
                    blocked.append(model_id)
                    kept.append(item)
                elif model_ids is None or model_id in model_ids:
                    removed.append(model_id)
                else:
                    kept.append(item)
            if removed:
                config["models"] = kept
                save_config(config)
    if not removed:
        if blocked:
            return f"没有可清理的模型：{len(blocked)} 个网关未列出的模型仍被实例使用，请先切换实例模型"
        return "没有需要清理的模型：当前手动模型都在网关列表中"
    message = f"已清理 {len(removed)} 个网关未列出的模型：{'、'.join(removed)}"
    if blocked:
        message += f"；{len(blocked)} 个模型仍被实例使用，未清理"
    return message


def auto_sync_models() -> None:
    """Refresh the model catalog from the gateway in the background."""
    try:
        message = sync_models_into_config()
    except ValueError as exc:
        panel_log("Model auto-sync skipped: " + str(exc))
        return
    except Exception as exc:  # pragma: no cover - defensive
        panel_log(f"Model auto-sync failed: {type(exc).__name__}: {exc}")
        return
    panel_log("Model auto-sync: " + message)


def start_model_auto_sync() -> None:
    def runner() -> None:
        time.sleep(1.5)
        auto_sync_models()

    threading.Thread(target=runner, daemon=True, name="model-auto-sync").start()


def log_level_options(current: str) -> str:
    return "".join(
        f'<option value="{level}" {"selected" if level == current else ""}>{level}</option>'
        for level in LOG_LEVELS
    )


def initials(name: str) -> str:
    cleaned = "".join(ch for ch in name.strip() if not ch.isspace())
    if not cleaned:
        return "PX"
    ascii_letters = "".join(ch for ch in cleaned if ch.isascii() and ch.isalnum())
    if ascii_letters:
        return ascii_letters[:3].upper()
    return cleaned[:2]


def toast_html(message: str) -> str:
    if not message:
        return ""
    kind = "error" if any(word in message for word in ("失败", "错误", "无效", "不能", "占用")) else "success"
    icon = "!" if kind == "error" else "✓"
    return f'<div class="toast {kind}"><strong>{icon}</strong><span>{esc(message)}</span></div>'


def sidebar(active: str) -> str:
    links = [
        ("instances", "/", "⌁", "代理实例"),
        ("logs", "/logs", "◫", "运行日志"),
        ("models", "/models", "◇", "模型配置"),
        ("settings", "/settings", "⚙", "全局设置"),
    ]
    rendered = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}"><span class="nav-icon">{icon}</span>{label}</a>'
        for key, href, icon, label in links
    )
    return f'''<aside class="sidebar">
<div class="brand"><div class="brand-mark">OC</div><div><strong>OpenCANNBot</strong><span>代理中心</span></div></div>
<div class="nav-label">Workspace</div><nav class="nav" aria-label="主要导航">{rendered}</nav>
<div class="sidebar-bottom"><div class="system-card"><div class="system-line"><span class="live-dot"></span>控制服务在线</div><small>仅监听 127.0.0.1<br>最多运行 3 个独立实例</small></div></div>
</aside>'''


def page_shell(title: str, body: str, *, active: str = "instances", message: str = "", script: str = "") -> str:
    script_tag = f'<script src="/static/{script}" defer></script>' if script else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'">
<meta name="description" content="OpenCANNBot 本地代理实例控制中心"><meta name="color-scheme" content="light"><meta name="theme-color" content="#f7f8fb">
<title>{esc(title)}</title><link rel="stylesheet" href="/static/proxy_center.css">{script_tag}</head><body><a class="skip-link" href="#main-content">跳到主要内容</a><div class="app">{sidebar(active)}<main class="main" id="main-content" tabindex="-1">
<header class="topbar"><div class="topbar-context"><div class="topbar-eyebrow">LOCAL CONTROL PLANE</div><div class="breadcrumb">OpenCANNBot / <strong>{esc(title)}</strong></div></div><div class="top-actions"><div class="topbar-status"><span class="topbar-status-dot"></span>本机服务</div><a class="btn secondary icon-btn" href="/settings" aria-label="打开全局设置" title="全局设置">⚙</a><form method="post" action="/system/exit"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button class="btn secondary" type="submit" onclick="return confirm('将停止所有由代理中心管理的实例并完全退出，确定继续吗？')">完全退出</button></form></div></header>
<div class="content">{toast_html(message)}{body}</div></main></div></body></html>'''


def public_instance(config: dict[str, Any], instance: dict[str, Any]) -> dict[str, Any]:
    state = instance_status(instance)
    vk = read_instance_vk(str(instance["id"]))
    return {
        "id": instance["id"],
        "name": instance["name"],
        "description": instance["description"],
        "port": int(instance["port"]),
        "model": instance["model"],
        "model_label": model_label(str(instance["model"]), config),
        "running": state["running"],
        "managed": state["managed"],
        "external": state["external"],
        "healthy": state["healthy"],
        "pid": state["pid"],
        "message": state["message"],
        "drift": state["drift"],
        "vk_configured": bool(vk),
        "vk_preview": mask_vk(vk),
        "health": state["health"],
        "endpoint": f"http://127.0.0.1:{instance['port']}",
    }

def dashboard_page(config: dict[str, Any], message: str = "") -> str:
    state = repo_state(config)
    items = [public_instance(config, instance) for instance in config.get("instances", [])]
    running_count = sum(1 for item in items if item["running"])
    healthy_count = sum(1 for item in items if item["healthy"])
    ports = [str(item["port"]) for item in items]
    model_names = list(dict.fromkeys(item["model_label"] for item in items))
    remaining = MAX_INSTANCES - len(items)

    cards: list[str] = []
    for item in items:
        instance = find_instance(config, str(item["id"]))
        assert instance is not None
        if item["external"]:
            status_class, status_text = "external", "外部运行"
        elif item["healthy"]:
            status_class, status_text = "running", "运行中"
        elif item["running"]:
            status_class, status_text = "unhealthy", "响应异常"
        else:
            status_class, status_text = "stopped", "已停止"
        card_class = "running" if item["running"] else "stopped"
        start_disabled = "disabled" if item["running"] or not state["ready"] or not item["vk_configured"] else ""
        restart_disabled = "disabled" if not item["managed"] else ""
        stop_disabled = "disabled" if not item["managed"] else ""
        apply_label = "应用并重启" if item["running"] else "应用并启动"
        cards.append(f'''
<article class="instance-card {card_class}" data-instance-card="{esc(item['id'])}">
  <div class="instance-main">
    <div class="instance-title-row">
      <div class="instance-name"><div class="instance-avatar {'flash' if item['model'] in ('glm-5.3-flash', 'deepseek-v4-flash') else ''}">{esc(initials(item['name']))}</div><div><h3>{esc(item['name'])}</h3><p>{esc(item['description'] or '未填写用途说明')}</p></div></div>
      <span class="status-pill {status_class}" data-role="status">{status_text}</span>
    </div>
    <div class="instance-meta">
      <div class="meta-box"><span>Endpoint</span><strong><code>:{item['port']}</code></strong></div>
      <div class="meta-box"><span>Model</span><strong data-role="model">{esc(item['model_label'])}</strong></div>
      <div class="meta-box"><span>Process</span><strong data-role="process">{('PID '+str(item['pid'])) if item['pid'] else ('外部进程' if item['external'] else '—')}</strong></div>
    </div>
  </div>
  <form method="post" action="/instances/{esc(item['id'])}/model" class="model-strip">
    <input type="hidden" name="csrf" value="{CSRF_TOKEN}">
    <div><label>快速切换模型</label><select name="model" data-model-select="{esc(item['id'])}" data-current="{esc(item['model'])}">{model_options(str(item['model']), config)}</select></div>
    <button class="btn soft small" data-model-button="{esc(item['id'])}" type="submit">{apply_label}</button>
  </form>
  <div class="instance-actions">
    <div class="action-group">
      <form method="post" action="/instances/{esc(item['id'])}/action"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button class="btn primary small" data-action="start" name="action" value="start" {start_disabled}>启动</button></form>
      <form method="post" action="/instances/{esc(item['id'])}/action"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button class="btn secondary small" data-action="restart" name="action" value="restart" {restart_disabled}>重启</button></form>
      <form method="post" action="/instances/{esc(item['id'])}/action"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button class="btn danger small" data-action="stop" name="action" value="stop" {stop_disabled} onclick="return confirm('确定停止该代理实例吗？')">停止</button></form>
      <a class="btn text small" href="/instances/{esc(item['id'])}/edit">配置</a>
    </div>
    <button class="copy-button" type="button" data-copy="{esc(item['endpoint'])}">{esc(item['endpoint'])}</button>
  </div>
  <div class="card-details"><details><summary>{esc(item['message'])}</summary><pre>{esc(item['health']['text'])}</pre></details></div>
</article>''')

    if remaining > 0:
        cards.append(f'''<article class="add-card"><div><div class="plus">＋</div><h3>添加第 {len(items)+1} 个代理实例</h3><p>为新的应用分配独立端口、固定模型和认证信息。</p><a class="btn primary small" href="/instances/new">创建实例</a></div></article>''')

    if not cards:
        cards.append('''<div class="empty-state"><div class="plus">＋</div><h3>还没有代理实例</h3><p>创建一个实例，为应用分配独立端口、模型和 VK。</p><a class="btn primary" href="/instances/new">创建第一个实例</a></div>''')

    repo_warning = ""
    if not state["ready"]:
        repo_warning = f'<div class="toast warning"><strong>!</strong><span>OpenCANNBot 项目目录尚未配置或缺少 cannbot-claude-proxy.py。<a href="/settings"><strong>立即配置</strong></a></span></div>'

    body = f'''
<section class="page-head"><div><h1>代理实例</h1><p>为不同应用创建独立端口和固定模型路由，实例之间互不影响。</p></div><div class="top-actions"><button class="btn secondary" id="refresh-button" type="button">刷新状态 <span class="kbd">R</span></button><a class="btn secondary" href="/models">模型配置</a><a class="btn primary {'' if remaining > 0 else 'hidden'}" href="/instances/new">＋ 新建实例</a></div></section>
{repo_warning}
<section class="stats">
  <div class="stat"><div class="stat-top"><span class="stat-label">实例总数</span><span class="stat-icon">▦</span></div><strong id="stat-total">{len(items)} / {MAX_INSTANCES}</strong><small id="stat-total-note">{'还可创建 '+str(remaining)+' 个实例' if remaining else '已达到实例上限'}</small></div>
  <div class="stat"><div class="stat-top"><span class="stat-label">运行中</span><span class="stat-icon green">●</span></div><strong id="stat-running">{running_count}</strong><small id="stat-running-note">{healthy_count} 个实例健康运行</small></div>
  <div class="stat"><div class="stat-top"><span class="stat-label">监听端口</span><span class="stat-icon cyan">⌁</span></div><strong id="stat-ports">{len(ports)}</strong><small id="stat-ports-note">{esc(' · '.join(ports) or '暂无监听端口')}</small></div>
  <div class="stat"><div class="stat-top"><span class="stat-label">模型路由</span><span class="stat-icon amber">◆</span></div><strong id="stat-models">{len(model_names)}</strong><small id="stat-models-note">{esc(' · '.join(model_names) or '暂无模型路由')}</small></div>
</section>
<div class="section-head"><div class="section-title"><h2>实例列表</h2><span id="auto-refresh-label">自动刷新 5s</span></div><a class="btn text" href="/logs">查看全部日志 →</a></div>
<section class="instance-grid">{''.join(cards)}</section>'''
    return page_shell("代理实例", body, active="instances", message=message, script="dashboard.js")


def instance_form_page(config: dict[str, Any], instance: Optional[dict[str, Any]], message: str = "") -> str:
    creating = instance is None
    if creating:
        if len(config.get("instances", [])) >= MAX_INSTANCES:
            return page_shell("新建实例", '<div class="toast error"><strong>!</strong><span>最多只能创建 3 个实例。</span></div>', active="instances")
        draft = new_instance_record(len(config.get("instances", [])))
        draft["port"] = next_available_port(config)
    else:
        draft = dict(instance)
    instance_id = str(draft["id"])
    vk = read_instance_vk(instance_id) if not creating else ""
    title = "新建代理实例" if creating else f"配置 {draft['name']}"
    action = "/instances/new" if creating else f"/instances/{esc(instance_id)}/save"
    primary_label = "创建并启动" if creating else "保存并重启"
    secondary_label = "仅创建" if creating else "仅保存"
    delete_zone = ""
    if not creating:
        delete_zone = f'''
<section class="panel danger-zone"><div class="panel-head"><div><h2>危险操作</h2><p>删除实例会移除该实例的 VK、PID 记录、启动器和日志。</p></div></div><div class="panel-body"><form method="post" action="/instances/{esc(instance_id)}/action"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button class="btn danger" type="submit" name="action" value="delete" onclick="return confirm('确定删除该实例及其本地配置吗？')">删除实例</button></form></div></section>'''
    body = f'''
<section class="page-head"><div><h1>{esc(title)}</h1><p>每个实例拥有独立端口、强制模型路由、VK、PID 和日志。</p></div><a class="btn secondary" href="/">返回实例列表</a></section>
<div class="form-layout">
  <div>
    <form method="post" action="{action}" class="panel form-panel">
      <input type="hidden" name="csrf" value="{CSRF_TOKEN}">
      <section class="form-section"><div class="section-kicker">Identity</div><h3>实例信息</h3><p>使用清晰名称标记该端口由哪个应用使用。</p><div class="fields">
        <div class="field"><label for="name">实例名称</label><input id="name" name="name" maxlength="40" value="{esc(draft['name'])}" required><small>例如 Claude Code、桌面助手、文档应用。</small></div>
        <div class="field"><label for="description">用途说明</label><input id="description" name="description" maxlength="100" value="{esc(draft['description'])}"><small>显示在实例卡片下方。</small></div>
      </div></section>
      <section class="form-section"><div class="section-kicker">Routing</div><h3>端口与模型</h3><p>该端口会忽略客户端提交的其他模型名，并固定路由到所选模型。</p><div class="fields">
        <div class="field"><label for="port">监听端口</label><input id="port" type="number" min="1" max="65535" name="port" value="{int(draft['port'])}" required><small>默认建议使用 8766、8767、8768，不能与其他实例重复。</small></div>
        <div class="field"><label for="model">固定模型</label><select id="model" name="model">{model_options(str(draft['model']), config)}</select><small>模型列表会自动从网关同步，也可在“模型配置”页面手动维护。</small></div>
      </div></section>
      <section class="form-section"><div class="section-kicker">Authentication</div><h3>实例认证</h3><p>VK 仅保存在当前用户目录，不会显示完整内容。</p><div class="fields">
        <div class="field full"><label for="vk">CANNBOT VK</label><input id="vk" type="password" name="vk" placeholder="{esc(mask_vk(vk)) if vk else 'vk-xxxxxxxxxxxxxxxx'}"><small>{'留空保留现有 VK。' if vk else '首次创建并启动前必须填写 VK。'}</small></div>
      </div></section>
      <section class="form-section"><div class="section-kicker">Runtime</div><h3>运行参数</h3><p>一般保持默认即可。变更后使用“保存并重启”应用。</p><div class="fields">
        <div class="field"><label for="log_level">日志级别</label><select id="log_level" name="log_level">{log_level_options(str(draft['log_level']))}</select></div>
        <div class="field"><label for="keepalive_idle">最大空闲等待（秒）</label><input id="keepalive_idle" type="number" min="30" max="3600" name="keepalive_idle" value="{int(draft['keepalive_idle'])}" required></div>
        <div class="field"><label for="socket_timeout">Socket 读取超时（秒）</label><input id="socket_timeout" type="number" min="5" max="300" name="socket_timeout" value="{int(draft['socket_timeout'])}" required></div>
      </div><div class="notice"><span>ⓘ</span><span>运行中的实例修改端口、模型、VK 或运行参数后，需要重启该实例。其他实例不会受到影响。</span></div></section>
      <div class="form-footer"><small>保存操作不会影响其他端口。</small><div class="top-actions"><a class="btn secondary" href="/">取消</a><button class="btn secondary" type="submit" name="submit_mode" value="save">{secondary_label}</button><button class="btn primary" type="submit" name="submit_mode" value="restart">{primary_label}</button></div></div>
    </form>
    {delete_zone}
  </div>
  <aside class="panel preview-card">
    <div class="preview-hero"><span class="tag">独立模型路由</span><h3 id="preview-name">{esc(draft['name'])}</h3><p id="preview-description">{esc(draft['description'] or '未填写用途说明')}</p><div class="preview-endpoint" id="preview-endpoint">http://127.0.0.1:{int(draft['port'])}</div></div>
    <div class="preview-list"><div class="preview-row"><span>固定模型</span><strong id="preview-model">{esc(model_label(str(draft['model']), config))}</strong></div><div class="preview-row"><span>认证状态</span><strong>{'已配置' if vk else '待配置'}</strong></div><div class="preview-row"><span>健康检查</span><strong>{'创建后可用' if creating else ('正常' if instance_status(draft)['healthy'] else '未运行')}</strong></div><div class="preview-row"><span>日志文件</span><strong class="inline-code">{esc(instance_log_path(instance_id).name)}</strong></div></div>
    <div class="preview-actions"><div class="copy-row"><code>ANTHROPIC_BASE_URL=http://127.0.0.1:{int(draft['port'])}</code></div><div class="copy-row"><code>ANTHROPIC_MODEL={esc(draft['model'])}</code></div></div>
  </aside>
</div>'''
    return page_shell(title, body, active="instances", message=message, script="edit.js")


def logs_page(config: dict[str, Any]) -> str:
    cards: list[str] = []
    for instance in config.get("instances", []):
        status_value = instance_status(instance)
        cards.append(f'''<section class="log-card"><div class="log-card-head"><div><h3>{esc(instance['name'])}</h3><span class="endpoint">127.0.0.1:{instance['port']} · {esc(model_label(str(instance['model']), config))}</span></div><span class="status-pill {'running' if status_value['healthy'] else 'stopped'}">{'运行中' if status_value['healthy'] else '未运行'}</span></div><pre>{esc(tail_file(instance_log_path(str(instance['id'])), 180))}</pre></section>''')
    if not cards:
        cards.append('<div class="empty-state"><h3>暂无实例日志</h3><p>创建并启动实例后，日志会显示在这里。</p></div>')
    body = f'''<section class="page-head"><div><h1>运行日志</h1><p>分别查看每个实例的启动、认证、请求与错误信息。</p></div><a class="btn secondary" href="/">返回代理实例</a></section><div class="log-grid">{''.join(cards)}</div>'''
    return page_shell("运行日志", body, active="logs")


def models_page(config: dict[str, Any], message: str = "") -> str:
    catalog = model_catalog(config)
    cache = read_models_cache()
    synced_label = format_timestamp(cache.get("synced_at"))
    attempt_label = format_timestamp(cache.get("last_attempt"))
    last_error = str(cache.get("last_error") or "").strip()
    cached_models = cache.get("models") if isinstance(cache.get("models"), list) else []
    cached_ids = {
        str(item.get("id", ""))
        for item in cached_models
        if isinstance(item, dict)
    }
    sync_source = str(cache.get("source") or "").strip()
    if synced_label:
        sync_state = f"上次同步 {synced_label}，网关返回 {len(cached_ids)} 个模型"
        sync_hint = f"同步来源：{sync_source}；网关模型会自动出现在实例的模型下拉列表中。"
    else:
        sync_state = "尚未同步"
        sync_hint = "点击右侧按钮立即获取；代理中心每次启动时也会自动同步一次。"
    if cache.get("complete") is False:
        sync_hint += " 上次同步未覆盖全部 VK，下架模型已保留。"
    sync_error_html = ""
    if last_error:
        sync_error_html = (
            f'<small class="model-sync-error">最近一次同步失败'
            f'{"（" + attempt_label + "）" if attempt_label else ""}：{esc(last_error)}；'
            f'当前保留上次成功同步的列表。</small>'
        )

    unlisted = gateway_unlisted_models(config, cached_ids) if cached_ids else []
    used_models = {str(instance.get("model", "")) for instance in config.get("instances", [])}
    removable = [model_id for model_id in unlisted if model_id not in used_models]
    prune_link = ""
    if removable:
        prune_link = f'<a class="btn secondary" href="/models/prune">清理未列出的模型（{len(removable)}）</a>'

    builtin_count = sum(1 for item in catalog if item.get("builtin"))
    gateway_count = sum(1 for item in catalog if not item.get("builtin") and str(item.get("source", "custom")).lower() == "gateway")
    custom_count = len(catalog) - builtin_count - gateway_count

    rows: list[str] = []
    for item in catalog:
        model_id = str(item["id"])
        label = str(item["label"])
        usage = sum(1 for instance in config.get("instances", []) if instance.get("model") == model_id)
        is_builtin = bool(item.get("builtin"))
        source = str(item.get("source", "custom")).lower()
        if is_builtin:
            badge_class, badge_text = "builtin", "内置"
        elif source == "gateway":
            badge_class, badge_text = "gateway", "网关同步"
        else:
            badge_class, badge_text = "custom", "自定义"
        usage_text = f"当前有 {usage} 个实例使用" if usage else "暂无实例使用"
        if not is_builtin and cached_ids and model_id not in cached_ids:
            usage_text += " · 网关列表未包含"
        delete_form = f'''<form method="post" action="/models/delete" class="model-delete-form"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><input type="hidden" name="model_id" value="{esc(model_id)}"><button class="btn danger small" type="submit" onclick="return confirm('确定删除模型 {esc(model_id)} 吗？')">删除</button></form>'''
        if is_builtin:
            actions = '<span class="model-badge builtin">内置模型</span>'
        elif source == "gateway":
            actions = delete_form
        else:
            save_form = f'''<form method="post" action="/models/save" class="model-inline-form"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><input type="hidden" name="model_id" value="{esc(model_id)}"><input name="label" maxlength="{MODEL_LABEL_MAX_LENGTH}" value="{esc(label)}" aria-label="{esc(model_id)} 显示名称"><button class="btn secondary small" type="submit">保存名称</button></form>'''
            actions = save_form + delete_form
        rows.append(f'''
<div class="model-row"><div class="model-row-info"><div class="model-row-title"><code>{esc(model_id)}</code><span class="model-badge {badge_class}">{badge_text}</span></div><strong>{esc(label)}</strong><p>{usage_text}</p></div><div class="model-row-actions">{actions}</div></div>''')

    body = f'''
<section class="page-head"><div><h1>模型配置</h1><p>管理实例可选的模型路由。模型 ID 会原样发送到 OpenCANNBot 网关。</p></div><div class="top-actions"><a class="btn secondary" href="/">返回代理实例</a><a class="btn secondary" href="/settings">全局设置</a></div></section>
<section class="panel model-sync-panel">
  <div class="panel-head"><div><h2>网关模型同步</h2><p>直接读取 OpenCANNBot 网关的模型配置，自动更新可选模型，无需手动填写模型 ID。</p></div><span class="model-badge gateway">自动</span></div>
  <div class="panel-body model-sync-body">
    <div class="model-sync-state"><strong>{sync_state}</strong><small>{esc(sync_hint)}</small>{sync_error_html}</div>
    <div class="top-actions">
      <form method="post" action="/models/sync"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button class="btn primary" type="submit">从网关同步模型</button></form>
      {prune_link}
    </div>
  </div>
  <div class="model-sync-note"><div class="notice"><span>ⓘ</span><span>同步使用实例中已配置的 VK（没有实例 VK 时回退到全局 VK）调用网关接口，只加入状态为启用的模型；网关未提供的模型会在同步成功后自动移除（仍被实例使用的除外）。每次启动代理中心也会自动同步一次。</span></div></div>
</section>
<div class="model-admin-grid">
  <section class="panel"><div class="panel-head"><div><h2>添加自定义模型</h2><p>填写网关实际接受的模型 ID，不要填写展示名称。</p></div></div><form method="post" action="/models/save" class="panel-body"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><div class="fields"><div class="field"><label for="model_id">模型 ID</label><input id="model_id" name="model_id" maxlength="128" pattern="[A-Za-z0-9][A-Za-z0-9._:/-]*" placeholder="例如 custom-model-v1" required><small>只能使用字母、数字、点、下划线、冒号、斜线和连字符。</small></div><div class="field"><label for="model_label">显示名称</label><input id="model_label" name="label" maxlength="{MODEL_LABEL_MAX_LENGTH}" placeholder="例如 Custom Model V1"><small>显示在实例配置和快速切换列表中。</small></div></div><div class="notice"><span>ⓘ</span><span>新增模型不会绕过网关权限；模型必须已被你的 VK 授权。保存后请在实例页面选择并重启。</span></div><div class="top-actions form-actions"><button class="btn primary" type="submit">添加模型</button></div></form></section>
  <section class="panel"><div class="panel-head"><div><h2>可用模型</h2><p>{len(catalog)} 个模型：{builtin_count} 个内置、{gateway_count} 个网关同步、{custom_count} 个手动添加。</p></div></div><div class="model-list">{''.join(rows)}</div></section>
</div>'''
    return page_shell("模型配置", body, active="models", message=message)


def prune_confirm_page(config: dict[str, Any], message: str = "") -> str:
    cache = read_models_cache()
    cached_models = cache.get("models") if isinstance(cache.get("models"), list) else []
    cached_ids = {
        str(item.get("id", ""))
        for item in cached_models
        if isinstance(item, dict)
    }
    back_link = '<a class="btn secondary" href="/models">返回模型配置</a>'
    if not cached_ids:
        body = f'''<section class="page-head"><div><h1>清理未列出的模型</h1><p>需要先完成一次网关同步才能识别不在网关列表中的模型。</p></div>{back_link}</section>
<div class="toast error"><strong>!</strong><span>尚未获取网关模型列表，请先在“模型配置”页面点击“从网关同步模型”。</span></div>'''
        return page_shell("清理未列出的模型", body, active="models", message=message)
    if cache.get("complete") is False:
        body = f'''<section class="page-head"><div><h1>清理未列出的模型</h1><p>上次同步未覆盖全部 VK，无法确定哪些模型真正下架。</p></div>{back_link}</section>
<div class="toast error"><strong>!</strong><span>上次同步不完整（部分 VK 查询失败），请先在“模型配置”页面重新同步，再进行清理。</span></div>'''
        return page_shell("清理未列出的模型", body, active="models", message=message)

    catalog = model_catalog(config)
    labels = {str(item["id"]): str(item["label"]) for item in catalog}
    used = {str(instance.get("model", "")) for instance in config.get("instances", [])}
    unlisted = gateway_unlisted_models(config, cached_ids)
    removable = [model_id for model_id in unlisted if model_id not in used]
    blocked = [model_id for model_id in unlisted if model_id in used]
    rows = "".join(
        f'<div class="model-row"><div class="model-row-info"><div class="model-row-title"><code>{esc(model_id)}</code><span class="model-badge custom">手动添加</span></div><strong>{esc(labels.get(model_id, model_id))}</strong></div></div>'
        for model_id in removable
    )
    if not rows:
        rows = '<div class="model-row"><div class="model-row-info"><p>没有可清理的模型。</p></div></div>'
    blocked_html = ""
    if blocked:
        blocked_html = f'<div class="notice"><span>ⓘ</span><span>{len(blocked)} 个模型不在网关列表中但仍被实例使用，已跳过：{esc("、".join(blocked))}</span></div>'
    stale_html = ""
    synced_label = format_timestamp(cache.get("synced_at"))
    try:
        synced_at = int(cache.get("synced_at") or 0)
    except (TypeError, ValueError):
        synced_at = 0
    if synced_at and time.time() - synced_at > MODELS_SYNC_MAX_AGE:
        stale_html = f'<div class="notice"><span>ⓘ</span><span>上次同步时间为 {esc(synced_label or "未知")}，已超过 24 小时；建议先回到“模型配置”重新同步，确认模型确实下架后再清理。</span></div>'
    form_html = ""
    if removable:
        form_html = f'''<form method="post" action="/models/prune"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><input type="hidden" name="model_ids" value="{esc(",".join(removable))}"><div class="top-actions form-actions"><a class="btn secondary" href="/models">取消</a><button class="btn danger" type="submit">确认清理这 {len(removable)} 个模型</button></div></form>'''
    heading = f"将删除 {len(removable)} 个模型" if removable else "没有可清理的模型"
    body = f'''
<section class="page-head"><div><h1>清理未列出的模型</h1><p>以下手动模型不在最近一次网关同步结果中，可能已下架或未被当前 VK 授权。</p></div>{back_link}</section>
<section class="panel"><div class="panel-head"><div><h2>{heading}</h2><p>内置模型和网关同步模型不会被删除；被实例使用的模型会自动跳过。</p></div></div><div class="model-list">{rows}</div><div class="panel-body">{stale_html}{blocked_html}{form_html}</div></section>'''
    return page_shell("清理未列出的模型", body, active="models", message=message)


def settings_page(config: dict[str, Any], message: str = "") -> str:
    state = repo_state(config)
    body = f'''
<section class="page-head"><div><h1>全局设置</h1><p>配置 OpenCANNBot 仓库位置和查看代理中心运行目录。</p></div><div class="top-actions"><a class="btn secondary" href="/models">模型配置</a><a class="btn secondary" href="/">返回代理实例</a></div></section>
<div class="settings-grid">
  <form method="post" action="/settings" class="panel"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><div class="panel-head"><div><h2>OpenCANNBot 项目</h2><p>代理中心会调用仓库中的 cannbot-claude-proxy.py。</p></div></div><div class="panel-body"><div class="field"><label for="repo_dir">仓库根目录</label><input id="repo_dir" name="repo_dir" value="{esc(config.get('repo_dir',''))}" placeholder="C:\\Projects\\opencannbot" required><small>保存时会检查文件是否存在。</small></div><div class="notice"><span>ⓘ</span><span>修改仓库位置不会自动重启现有实例。请分别重启需要应用新代码的实例。</span></div><div class="top-actions form-actions"><button class="btn primary" type="submit">保存全局设置</button></div></div></form>
  <aside class="panel"><div class="panel-head"><div><h2>运行信息</h2><p>排查安装、数据和日志位置。</p></div></div><div class="panel-body"><div class="preview-row"><span>仓库状态</span><strong>{'已就绪' if state['ready'] else '未就绪'}</strong></div><div class="preview-row"><span>实例上限</span><strong>{MAX_INSTANCES}</strong></div><div class="preview-row"><span>页面端口</span><strong>{PANEL_PORT}</strong></div><div class="field"><label>程序目录</label><div class="path-card">{esc(PROGRAM_DIR)}</div></div><div class="field"><label>数据目录</label><div class="path-card">{esc(APP_DIR)}</div></div><div class="field"><label>面板日志</label><div class="path-card">{esc(PANEL_LOG_FILE)}</div></div></div></aside>
</div>'''
    return page_shell("全局设置", body, active="settings", message=message)


def restart_page(job: dict[str, Any]) -> str:
    config = load_config()
    instance = find_instance(config, str(job.get("instance_id", "")))
    port = instance.get("port") if instance else "—"
    model = model_label(str(instance.get("model")), config) if instance else "—"
    steps = "".join(
        f'''<div class="timeline-item {'done' if step.get('state')=='done' else 'active' if step.get('state')=='active' else ''}" data-step-index="{index}"><div class="step-circle">{index+1}</div><div><h3>{esc(step.get('label',''))}</h3><p data-role="step-detail">{esc(step.get('detail',''))}</p></div><span class="step-state" data-role="step-state">{esc(step.get('state_label','等待中'))}</span></div>'''
        for index, step in enumerate(job.get("steps", []))
    )
    body = f'''<div class="restart-shell" data-job-id="{esc(job['id'])}"><section class="restart-card" id="restart-card"><div class="restart-head"><div class="restart-ring"><span id="job-percent">{int(job.get('progress',0))}%</span></div><div><h1 id="job-title">{esc(job.get('title','正在重启代理实例'))}</h1><p id="job-message">{esc(job.get('message','请稍候'))}</p></div><span class="restart-chip" id="job-chip">正在执行</span></div><div class="restart-body"><div class="timeline">{steps}<div class="job-error" id="job-error"></div></div><aside class="restart-side"><h3>任务详情</h3><div class="restart-meta"><div><span>实例</span><strong>{esc(job.get('instance_name',''))}</strong></div><div><span>监听端口</span><strong>{esc(port)}</strong></div><div><span>目标模型</span><strong>{esc(model)}</strong></div><div><span>已用时间</span><strong id="job-duration">0.0 秒</strong></div></div><div class="log-box" id="job-log">等待任务日志...</div></aside></div><div class="restart-foot"><span>成功后将自动返回代理中心。</span><div class="restart-actions"><a class="btn secondary hidden" id="job-retry" href="/">返回代理中心</a><div class="progress"><i id="job-progress"></i></div></div></div></section></div>'''
    return page_shell("实例任务", body, active="instances", script="job.js")


def exit_page() -> str:
    body = '''<div class="restart-shell"><section class="restart-card"><div class="restart-head"><div class="restart-ring"><span>100%</span></div><div><h1>代理中心正在完全退出</h1><p>正在停止所有由代理中心管理的实例并关闭页面服务。</p></div><span class="restart-chip">正在关闭</span></div><div class="panel-body"><p>此页面不会继续刷新，可以直接关闭浏览器标签页。重新使用时运行 start-proxy-center.vbs。</p></div></section></div>'''
    return page_shell("正在退出", body, active="instances")

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "OpenCANNBotProxyCenter/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        panel_log("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_body(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_body(text.encode("utf-8"), status, content_type)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 131072:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def check_csrf(self, form: dict[str, str]) -> None:
        if not secrets.compare_digest(form.get("csrf", ""), CSRF_TOKEN):
            raise ValueError("页面令牌无效，请刷新页面后重试")

    def serve_static(self, filename: str) -> None:
        allowed = {
            "proxy_center.css": STYLE_FILE,
            "dashboard.js": DASHBOARD_JS_FILE,
            "job.js": JOB_JS_FILE,
            "edit.js": EDIT_JS_FILE,
        }
        path = allowed.get(filename)
        if not path or not path.is_file():
            self.send_text("404", 404, "text/plain; charset=utf-8")
            return
        mime, _ = mimetypes.guess_type(str(path))
        self.send_body(path.read_bytes(), 200, mime or "application/octet-stream")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        message = query.get("message", [""])[-1]
        config = load_config()

        if path.startswith("/static/"):
            self.serve_static(path.split("/", 2)[-1])
            return
        if path == "/":
            self.send_text(dashboard_page(config, message))
            return
        if path == "/instances/new":
            self.send_text(instance_form_page(config, None, message))
            return
        match = re.fullmatch(r"/instances/([a-z0-9-]+)/edit", path)
        if match:
            instance = find_instance(config, match.group(1))
            if not instance:
                self.send_text("实例不存在", 404, "text/plain; charset=utf-8")
                return
            self.send_text(instance_form_page(config, instance, message))
            return
        if path == "/logs":
            self.send_text(logs_page(config))
            return
        if path == "/models":
            self.send_text(models_page(config, message))
            return
        if path == "/models/prune":
            self.send_text(prune_confirm_page(config, message))
            return
        if path == "/settings":
            self.send_text(settings_page(config, message))
            return
        match = re.fullmatch(r"/jobs/([a-f0-9]{32})", path)
        if match:
            job = public_job(match.group(1))
            if not job:
                self.send_text("任务不存在", 404, "text/plain; charset=utf-8")
                return
            self.send_text(restart_page(job))
            return
        if path == "/api/status":
            state = repo_state(config)
            instances = [public_instance(config, item) for item in config.get("instances", [])]
            models = list(dict.fromkeys(item["model"] for item in instances))
            ports = [item["port"] for item in instances]
            payload = {
                "repo_ready": state["ready"],
                "repo_dir": str(state["repo"]),
                "instances": instances,
                "summary": {
                    "total": len(instances),
                    "max": MAX_INSTANCES,
                    "remaining": MAX_INSTANCES - len(instances),
                    "running": sum(1 for item in instances if item["running"]),
                    "healthy": sum(1 for item in instances if item["healthy"]),
                    "ports": ports,
                    "models": models,
                    "model_labels": [model_label(model, config) for model in models],
                },
            }
            self.send_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", content_type="application/json; charset=utf-8")
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path)
        if match:
            job = public_job(match.group(1))
            if not job:
                self.send_text(json.dumps({"error": "job_not_found"}), 404, "application/json; charset=utf-8")
                return
            self.send_text(json.dumps(job, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8")
            return
        match = re.fullmatch(r"/api/instances/([a-z0-9-]+)/log", path)
        if match:
            instance = find_instance(config, match.group(1))
            if not instance:
                self.send_text("实例不存在", 404, "text/plain; charset=utf-8")
                return
            self.send_text(tail_file(instance_log_path(match.group(1)), 250), content_type="text/plain; charset=utf-8")
            return
        self.send_text("404", 404, "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        # Serialize read-modify-write requests with the background model sync so
        # a stale handler snapshot cannot drop newly synced models. Sync/prune
        # take their own locks (MODELS_SYNC_LOCK before CONFIG_WRITE_LOCK) to
        # keep a single global lock order and avoid deadlocks.
        path = unquote(urlparse(self.path).path)
        if path in ("/models/sync", "/models/prune"):
            self._handle_post()
        else:
            with CONFIG_WRITE_LOCK:
                self._handle_post()

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            form = self.read_form()
            self.check_csrf(form)
            config = load_config()

            if path == "/models/save":
                message = upsert_model(config, form.get("model_id", ""), form.get("label", ""))
                save_config(config)
                self.redirect("/models?message=" + quote(message))
                return

            if path == "/models/delete":
                message = remove_model(config, form.get("model_id", "").strip())
                save_config(config)
                self.redirect("/models?message=" + quote(message))
                return

            if path == "/models/sync":
                message = sync_models_into_config()
                self.redirect("/models?message=" + quote(message))
                return

            if path == "/models/prune":
                confirmed = {
                    value.strip()
                    for value in form.get("model_ids", "").split(",")
                    if value.strip()
                }
                message = prune_unlisted_models(confirmed or None)
                self.redirect("/models?message=" + quote(message))
                return

            if path == "/settings":
                repo_text = form.get("repo_dir", "").strip()
                repo = Path(repo_text).expanduser().resolve()
                if not repo.is_dir() or not (repo / "cannbot-claude-proxy.py").is_file():
                    raise ValueError("仓库目录无效，未找到 cannbot-claude-proxy.py")
                config["repo_dir"] = str(repo)
                save_config(config)
                self.redirect("/settings?message=" + quote("全局设置已保存；请按需重启实例以应用仓库代码"))
                return

            if path == "/instances/new":
                if len(config.get("instances", [])) >= MAX_INSTANCES:
                    raise ValueError("最多只能创建 3 个代理实例")
                draft, vk = parse_instance_form(form, None, config)
                update_instance_in_config(config, draft)
                save_config(config)
                if vk:
                    save_instance_vk(str(draft["id"]), vk)
                write_instance_launchers(draft)
                if form.get("submit_mode") == "restart":
                    job_id = create_job(draft, "create", f"正在创建并启动 {draft['name']}")
                    self.redirect(f"/jobs/{job_id}")
                else:
                    self.redirect("/?message=" + quote("实例已创建；尚未启动"))
                return

            match = re.fullmatch(r"/instances/([a-z0-9-]+)/save", path)
            if match:
                instance = find_instance(config, match.group(1))
                if not instance:
                    raise ValueError("实例不存在")
                updated, vk = parse_instance_form(form, instance, config)
                update_instance_in_config(config, updated)
                save_config(config)
                if vk:
                    save_instance_vk(str(updated["id"]), vk)
                write_instance_launchers(updated)
                if form.get("submit_mode") == "restart":
                    job_id = create_job(updated, "save_restart", f"正在应用 {updated['name']} 的配置")
                    self.redirect(f"/jobs/{job_id}")
                else:
                    self.redirect("/?message=" + quote("实例配置已保存；运行中的旧进程不会自动变化"))
                return

            match = re.fullmatch(r"/instances/([a-z0-9-]+)/model", path)
            if match:
                instance = find_instance(config, match.group(1))
                if not instance:
                    raise ValueError("实例不存在")
                model = form.get("model", "").strip()
                if model not in model_ids(model_catalog(config)):
                    raise ValueError("请选择已配置的模型")
                instance["model"] = model
                update_instance_in_config(config, instance)
                save_config(config)
                write_instance_launchers(instance)
                job_id = create_job(instance, "model", f"正在将 {instance['name']} 切换到 {model_label(model, config)}")
                self.redirect(f"/jobs/{job_id}")
                return

            match = re.fullmatch(r"/instances/([a-z0-9-]+)/action", path)
            if match:
                instance = find_instance(config, match.group(1))
                if not instance:
                    raise ValueError("实例不存在")
                action = form.get("action", "")
                if action == "start":
                    job_id = create_job(instance, "start", f"正在启动 {instance['name']}")
                    self.redirect(f"/jobs/{job_id}")
                    return
                if action == "restart":
                    job_id = create_job(instance, "restart", f"正在重启 {instance['name']}")
                    self.redirect(f"/jobs/{job_id}")
                    return
                if action == "stop":
                    code, message = stop_instance(instance)
                    self.redirect("/?message=" + quote(f"[{code}] {message}"))
                    return
                if action == "delete":
                    code, message = delete_instance(config, instance)
                    self.redirect("/?message=" + quote(f"[{code}] {message}"))
                    return
                raise ValueError("不支持的实例操作")

            if path == "/system/exit":
                self.send_text(exit_page())
                threading.Thread(target=shutdown_center, args=(self.server,), daemon=True).start()
                return

            self.send_text("404", 404, "text/plain; charset=utf-8")
        except ValueError as exc:
            target = "/"
            if path == "/settings":
                target = "/settings"
            elif path.startswith("/models"):
                target = "/models"
            elif path == "/instances/new":
                target = "/instances/new"
            else:
                match = re.match(r"/instances/([a-z0-9-]+)", path)
                if match and find_instance(load_config(), match.group(1)):
                    target = f"/instances/{match.group(1)}/edit"
            self.redirect(target + "?message=" + quote("操作失败：" + str(exc)))
        except Exception as exc:
            panel_log(f"POST {path} failed: {type(exc).__name__}: {exc}")
            self.redirect("/?message=" + quote(f"内部错误：{type(exc).__name__}: {exc}"))


def shutdown_center(server: ThreadingHTTPServer) -> None:
    config = load_config()
    messages = stop_all_instances(config)
    for message in messages:
        panel_log("Full exit: " + message)
    time.sleep(0.3)
    server.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenCANNBot multi-instance proxy center")
    parser.add_argument("--port", type=int, default=8788, help="Control center port, default 8788")
    parser.add_argument("--repo-dir", help="Existing OpenCANNBot repository directory")
    parser.add_argument("--init-only", action="store_true", help="Initialize configuration and exit")
    parser.add_argument("--stop-all", action="store_true", help="Stop all managed proxy instances and exit")
    return parser.parse_args()


def initialize(repo_dir: Optional[str]) -> dict[str, Any]:
    ensure_app_dirs()
    config = load_config()
    if repo_dir:
        repo = Path(repo_dir).expanduser().resolve()
        if not repo.is_dir() or not (repo / "cannbot-claude-proxy.py").is_file():
            raise SystemExit(f"Invalid OpenCANNBot repository: {repo}")
        config["repo_dir"] = str(repo)
    if not config.get("instances"):
        first = new_instance_record(0, instance_id="proxy-default")
        first["name"] = "默认代理"
        first["description"] = "首个独立模型路由"
        config["instances"] = [first]
    save_config(config)
    write_all_launchers(config)
    return config


def main() -> None:
    global PANEL_PORT
    args = parse_args()
    PANEL_PORT = int(args.port)
    if not 1 <= PANEL_PORT <= 65535:
        raise SystemExit("Control center port must be between 1 and 65535")
    config = initialize(args.repo_dir)
    if args.stop_all:
        for message in stop_all_instances(config):
            print(message)
        return
    if args.init_only:
        state = repo_state(config)
        if not state["ready"]:
            raise SystemExit("OpenCANNBot repository is not configured or incomplete")
        print(f"Configuration: {CONFIG_FILE}")
        print(f"Instances: {len(config.get('instances', []))}/{MAX_INSTANCES}")
        return
    server = ThreadingHTTPServer(("127.0.0.1", PANEL_PORT), Handler)
    server.daemon_threads = True
    panel_log(f"Proxy center started at http://127.0.0.1:{PANEL_PORT}")
    # Refresh the model catalog from the gateway on startup so new models are
    # selectable without editing the configuration by hand.
    start_model_auto_sync()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        panel_log("Proxy center interrupted")
    finally:
        server.server_close()
        panel_log("Proxy center stopped")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        panel_log(f"Fatal error: {type(exc).__name__}: {exc}")
        raise
