"""The AniList done page must not reflect query values as markup."""

from __future__ import annotations

from fastapi.responses import HTMLResponse

from src.Web.Routes.Auth import anilist_done

PAYLOAD = "</script><script>alert(1)</script><img src=x onerror=alert(2)>"


def _body(response: HTMLResponse) -> str:
    return bytes(response.body).decode()


async def test_error_value_is_escaped() -> None:
    response = await anilist_done(request=None, error=PAYLOAD)  # type: ignore[arg-type]
    body = _body(response)
    assert "<script>alert(1)" not in body
    assert "<img src=x" not in body
    assert "&lt;img src=x" in body


async def test_username_and_user_id_are_escaped() -> None:
    response = await anilist_done(
        request=None, username=PAYLOAD, user_id=PAYLOAD  # type: ignore[arg-type]
    )
    body = _body(response)
    assert "<script>alert(1)" not in body
    assert "<img src=x" not in body
    assert "\\u003c/script\\u003e" in body
