"""Same-origin guard that runs before FastAPI reads a request body."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class SameOriginRoute(APIRoute):
    async def validate_request(self, request: Request) -> None:
        if request.method not in _SAFE_METHODS and request.headers.getlist(
            "origin"
        ) != [request.app.state.public_origin]:
            raise HTTPException(status_code=403)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            await self.validate_request(request)
            return await handle(request)

        return guarded
