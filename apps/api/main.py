from __future__ import annotations

import uvicorn

from .app import create_app
from .settings import AppSettings

app = create_app()


if __name__ == "__main__":
    settings = AppSettings.from_env()
    uvicorn.run(
        "apps.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level="info",
    )
