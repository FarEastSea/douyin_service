"""在隔离 xhs-api 上挂载本项目的作者主页采集入口。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from uvicorn import Config, Server
from xhs_adapters.config import AppSettings
from xhs_adapters.logging import configure_logging
from xhs_api.app import create_api

from xhs_profile_collector import collect_profile


def _create_app(settings: AppSettings, settings_file: Path):
    api = create_api(
        settings,
        settings_file=settings_file,
        settings_override_fields={"server_host", "server_port"},
    )
    collection_lock = asyncio.Lock()

    @api.post("/integrations/profile/collect", include_in_schema=False)
    async def collect(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=403, detail="作者采集入口仅允许本机访问")
        async with collection_lock:
            try:
                return await collect_profile(payload)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=str(exc)[:500]) from exc

    return api


async def _serve(host: str, port: int) -> None:
    settings_file = Path(".env")
    settings = AppSettings.from_env(
        settings_file,
        server_host=host,
        server_port=port,
    )
    configure_logging(settings.log_level)
    server = Server(
        Config(
            _create_app(settings, settings_file),
            host=host,
            port=port,
            log_level=settings.log_level,
            log_config=None,
        )
    )
    await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5556)
    args = parser.parse_args()
    asyncio.run(_serve(args.host, args.port))


if __name__ == "__main__":
    main()
