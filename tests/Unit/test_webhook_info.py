"""/api/webhook/info builds the Sonarr and Radarr URLs from the base URL."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.Utils.Config import AppConfig
from src.Web.Routes.ArrWebhook import webhook_info


async def test_webhook_info_uses_base_url() -> None:
    config = AppConfig(base_url="http://nas.local:9876/")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))
    response = await webhook_info(request)  # type: ignore[arg-type]
    assert json.loads(bytes(response.body)) == {
        "sonarr": "http://nas.local:9876/api/webhook/sonarr",
        "radarr": "http://nas.local:9876/api/webhook/radarr",
    }
