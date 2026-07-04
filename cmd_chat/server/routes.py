from sanic import Sanic, Request, Websocket

from . import views


def register_routes(app: Sanic) -> None:
    @app.post("/srp/init")
    async def srp_init_route(request: Request):
        return await views.srp_init(request, app)

    @app.post("/srp/verify")
    async def srp_verify_route(request: Request):
        return await views.srp_verify(request, app)

    @app.websocket("/ws/chat")
    async def chat_ws_route(request: Request, ws: Websocket):
        await views.chat_ws(request, ws, app)

    @app.get("/health")
    async def health_route(request: Request):
        return await views.health(request, app)

    @app.delete("/clear")
    async def clear_route(request: Request):
        return await views.clear_messages(request, app)
