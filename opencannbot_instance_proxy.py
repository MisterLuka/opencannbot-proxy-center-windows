#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one isolated OpenCANNBot Anthropic-compatible proxy instance.

The upstream repository proxy normally forwards the model supplied by the
client. This wrapper fixes the model per local port so different applications
can use different model routes without relying on client-side model settings.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenCANNBot isolated proxy instance")
    parser.add_argument("--source", required=True, help="Path to cannbot-claude-proxy.py")
    parser.add_argument("--model", required=True, help="Forced upstream model")
    parser.add_argument("--vk-file", required=True, help="Instance-specific VK file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--keepalive-idle", default=300, type=int)
    parser.add_argument("--socket-timeout", default=30, type=int)
    return parser.parse_args()


def load_proxy_module(source: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("opencannbot_upstream_proxy", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load proxy module: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_vk(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def build_handler(module: ModuleType, forced_model: str):
    class ForcedModelHandler(module.ProxyHandler):  # type: ignore[attr-defined]
        def _handle_messages(self, body: bytes) -> None:
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                self._send_error(400, "invalid_request_error", "Malformed JSON body")
                return
            payload["model"] = forced_model
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            super()._handle_messages(encoded)

        def _handle_health(self) -> None:
            vk = module.resolve_vk()
            with module._jwt_lock:
                jwt_present = bool(module._cached_jwt)
                expires_in = int(module._cached_jwt_exp - time.time()) if jwt_present else 0
            self._send_json(200, {
                "status": "ok",
                "vk_configured": bool(vk),
                "vk_preview": (vk[:8] + "...") if vk else None,
                "jwt_cached": jwt_present,
                "jwt_expires_in": expires_in,
                "gateway": module.GATEWAY_URL,
                "keepalive_idle": self.server.keepalive_idle,
                "socket_timeout": self.server.socket_timeout,
                "configured_model": forced_model,
                "model_routing": "forced",
            })

    return ForcedModelHandler


def main() -> None:
    args = parse_args()
    source = Path(args.source).expanduser().resolve()
    vk_file = Path(args.vk_file).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Proxy source not found: {source}")
    if not 1 <= args.port <= 65535:
        raise SystemExit("Port must be between 1 and 65535")

    module = load_proxy_module(source)
    module._setup_logging(args.log_level)
    log = logging.getLogger("opencannbot-instance-wrapper")

    vk = read_vk(vk_file)
    if vk:
        module._runtime_vk = vk
        module.exchange_vk_for_jwt(vk)
    else:
        log.warning("No VK configured for this instance")

    handler = build_handler(module, args.model)
    server = module._Server(
        (args.host, args.port),
        handler,
        keepalive_idle=args.keepalive_idle,
        socket_timeout=args.socket_timeout,
    )

    def graceful(signum: int, _frame: Any) -> None:
        log.info("Caught signal %d, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, graceful)
        except (ValueError, OSError):
            pass

    log.info(
        "OpenCANNBot instance listening on http://%s:%d; forced model=%s",
        args.host,
        args.port,
        args.model,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("OpenCANNBot instance stopped")


if __name__ == "__main__":
    main()
