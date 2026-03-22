"""FastAPI server — REST + WebSocket endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .app_state import AppState
from .market_discovery import fetch_market_by_slug, search_markets
from .models import OrderType, Side

log = logging.getLogger(__name__)


class PlaceOrderRequest(BaseModel):
    token_id: str
    side: str              # "BUY" or "SELL"
    order_type: str        # "GTC", "GTD", "FOK", "FAK"
    size: float
    price: Optional[float] = None
    expiration: Optional[float] = None  # GTD: unix timestamp


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Polymarket Simulator")

    import os
    static_dir = os.path.join(os.path.dirname(__file__), "static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        with open(os.path.join(static_dir, "index.html")) as f:
            return HTMLResponse(f.read())

    # ── Market discovery ───────────────────────────────────────────

    @app.get("/api/markets/search")
    async def api_search_markets(q: str = "", limit: int = 20):
        results = await search_markets(q, limit)
        return [r.model_dump() for r in results]

    @app.get("/api/markets/{slug}")
    async def api_get_market(slug: str):
        info = await fetch_market_by_slug(slug)
        if not info:
            return {"error": "Market not found"}
        return info.model_dump()

    @app.post("/api/market/select")
    async def api_select_market(slug: str):
        info = await fetch_market_by_slug(slug)
        if not info:
            return {"error": "Market not found"}
        if not info.token_ids:
            return {"error": "No token IDs found"}
        await state.switch_market(info)
        return {"ok": True, "question": info.question, "outcomes": info.outcome_names}

    # ── Orders ─────────────────────────────────────────────────────

    @app.post("/api/orders")
    async def api_place_order(req: PlaceOrderRequest):
        if not state.market_info:
            return {"error": "No market selected"}

        token_name = state.market_info.token_to_name.get(req.token_id, "")
        book = state.books.get(req.token_id)
        if not book:
            return {"error": "Unknown token_id"}

        snapshot = book.snapshot()

        try:
            side = Side(req.side)
            order_type = OrderType(req.order_type)
        except ValueError as e:
            return {"error": str(e)}

        order, fills = state.order_manager.place_order(
            token_id=req.token_id,
            token_name=token_name,
            side=side,
            order_type=order_type,
            size=req.size,
            price=req.price,
            book=snapshot,
            expiration=req.expiration,
        )

        await state.broadcast("order_update", {
            "id": order.id,
            "token_name": order.token_name,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "price": order.price,
            "size": order.size,
            "filled_size": round(order.filled_size, 2),
            "avg_fill_price": round(order.avg_fill_price, 4),
            "status": order.status.value,
        })

        snapshots = state.get_book_snapshots()
        await state.broadcast("position", state.position.to_dict(snapshots))

        return {
            "order_id": order.id,
            "status": order.status.value,
            "filled_size": round(order.filled_size, 2),
            "avg_fill_price": round(order.avg_fill_price, 4),
            "fills": [{"price": f.price, "size": f.size, "notional": round(f.notional, 2)} for f in fills],
        }

    @app.delete("/api/orders/{order_id}")
    async def api_cancel_order(order_id: str):
        order = state.order_manager.cancel_order(order_id)
        if not order:
            return {"error": "Order not found"}
        await state.broadcast("order_update", {
            "id": order.id,
            "status": order.status.value,
        })
        return {"ok": True, "status": order.status.value}

    @app.get("/api/orders")
    async def api_get_orders():
        orders = state.order_manager.get_all_orders()
        return [{
            "id": o.id,
            "token_id": o.token_id,
            "token_name": o.token_name,
            "side": o.side.value,
            "order_type": o.order_type.value,
            "price": o.price,
            "size": o.size,
            "filled_size": round(o.filled_size, 2),
            "avg_fill_price": round(o.avg_fill_price, 4),
            "status": o.status.value,
            "created_at": o.created_at,
        } for o in orders]

    @app.get("/api/position")
    async def api_get_position():
        snapshots = state.get_book_snapshots()
        return state.position.to_dict(snapshots)

    @app.get("/api/fills")
    async def api_get_fills():
        fills = state.order_manager.get_recent_fills()
        return [{
            "order_id": f.order_id,
            "token_name": f.token_name,
            "side": f.side.value,
            "price": f.price,
            "size": f.size,
            "notional": round(f.notional, 2),
            "timestamp": f.timestamp,
        } for f in fills]

    @app.get("/api/snipes")
    async def api_get_snipes():
        events = state.sniping_detector.recent_events(300)
        return [{
            "token_name": e.token_name,
            "side": e.side,
            "price": e.price,
            "size_disappeared": e.size_disappeared,
            "duration_ms": round(e.duration_ms, 1),
            "timestamp": e.timestamp,
        } for e in events]

    @app.get("/api/queue")
    async def api_get_queue():
        return state.order_manager.get_queue_info()

    @app.get("/api/orders/{order_id}/timeline")
    async def api_order_timeline(order_id: str):
        """Full lifecycle timeline for an order — every state change."""
        order = state.order_manager.orders.get(order_id)
        if not order:
            return {"error": "Order not found"}
        return {
            "order_id": order.id,
            "token_name": order.token_name,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "price": order.price,
            "size": order.size,
            "filled_size": round(order.filled_size, 2),
            "status": order.status.value,
            "timeline": state.order_manager.get_order_timeline(order_id),
        }

    # ── Wallet tracking ────────────────────────────────────────────

    @app.get("/api/wallets")
    async def api_get_wallets(sort: str = "trade_count", limit: int = 30):
        return state.wallet_tracker.get_top_wallets(limit, sort)

    @app.get("/api/wallets/watched")
    async def api_get_watched():
        return state.wallet_tracker.get_watched_wallets()

    @app.post("/api/wallets/watch")
    async def api_watch_wallet(address: str):
        state.wallet_tracker.add_watch(address)
        return {"ok": True, "address": address.lower()}

    @app.delete("/api/wallets/watch")
    async def api_unwatch_wallet(address: str):
        state.wallet_tracker.remove_watch(address)
        return {"ok": True}

    @app.get("/api/wallets/{address}/trades")
    async def api_wallet_trades(address: str, limit: int = 50):
        return state.wallet_tracker.get_wallet_trades(address, limit)

    @app.get("/api/wallets/summary")
    async def api_wallet_summary():
        return state.wallet_tracker.get_summary()

    @app.get("/api/market-trades")
    async def api_market_trades(limit: int = 50):
        return state.wallet_tracker.get_recent_trades(limit)

    # ── Wallet subset books ────────────────────────────────────────

    @app.get("/api/wallet-books")
    async def api_list_wallet_books():
        return state.wallet_book_mgr.list_subsets()

    @app.post("/api/wallet-books")
    async def api_create_wallet_book(name: str, wallets: str):
        """Create a wallet subset book. wallets = comma-separated addresses."""
        wallet_list = [w.strip() for w in wallets.split(",") if w.strip()]
        if not wallet_list:
            return {"error": "No wallets provided"}
        subset = state.wallet_book_mgr.create_subset(name, wallet_list)
        return {"ok": True, "name": name, "wallet_count": len(subset.wallets)}

    @app.delete("/api/wallet-books/{name}")
    async def api_delete_wallet_book(name: str):
        state.wallet_book_mgr.remove_subset(name)
        return {"ok": True}

    @app.post("/api/wallet-books/{name}/wallets")
    async def api_add_wallet_to_book(name: str, address: str):
        state.wallet_book_mgr.add_wallet_to_subset(name, address)
        return {"ok": True}

    @app.delete("/api/wallet-books/{name}/wallets")
    async def api_remove_wallet_from_book(name: str, address: str):
        state.wallet_book_mgr.remove_wallet_from_subset(name, address)
        return {"ok": True}

    @app.get("/api/wallet-books/{name}/snapshot")
    async def api_wallet_book_snapshot(name: str, token_id: str = ""):
        subset = state.wallet_book_mgr.subsets.get(name)
        if not subset:
            return {"error": "Subset not found"}
        tid = token_id or (state.market_info.token_ids[0] if state.market_info else "")
        return subset.get_snapshot(tid)

    # ── Event recorder ─────────────────────────────────────────────

    @app.get("/api/recorder/status")
    async def api_recorder_status():
        return state.event_recorder.get_status()

    # ── Wallet inventory history (for graphs) ──────────────────────

    @app.get("/api/wallets/{address}/inventory")
    async def api_wallet_inventory(address: str):
        """Get inventory timeline for a wallet — for graphing."""
        addr = address.lower()
        ws = state.wallet_tracker.wallets.get(addr)
        if not ws:
            return {"address": addr, "timeline": []}

        # Build timeline from trades
        timeline = []
        running_pos = {}  # outcome -> tokens
        for t in ws.trades:
            outcome = t.get("outcome", "?")
            size = t.get("size", 0)
            side = t.get("side", "BUY")
            if side == "BUY":
                running_pos[outcome] = running_pos.get(outcome, 0) + size
            else:
                running_pos[outcome] = running_pos.get(outcome, 0) - size
            timeline.append({
                "timestamp": t.get("timestamp", 0),
                "time_str": t.get("time_str", ""),
                "positions": dict(running_pos),
                "trade": {"side": side, "outcome": outcome, "size": size, "price": t.get("price", 0)},
            })
        return {"address": addr, "timeline": timeline, "current": dict(running_pos)}

    @app.get("/api/status")
    async def api_status():
        return {
            "market": state.market_info.question if state.market_info else None,
            "ws_connected": state.market_ws.connected if state.market_ws else False,
            "open_orders": len(state.order_manager.gtc_matcher.entries),
            "trade_count": state.position.trade_count,
            "market_trades": state.wallet_tracker.total_trade_count,
            "unique_wallets": len(state.wallet_tracker.wallets),
        }

    # ── WebSocket for live UI updates ──────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        state.ws_clients.add(ws)
        log.info("UI client connected (%d total)", len(state.ws_clients))

        # Send current state
        if state.market_info:
            await ws.send_json({"type": "market_changed", "data": {
                "question": state.market_info.question,
                "slug": state.market_info.slug,
                "outcomes": state.market_info.outcome_names,
                "token_ids": state.market_info.token_ids,
                "condition_id": state.market_info.condition_id,
            }})

            for tid, book in state.books.items():
                snap = book.snapshot()
                await ws.send_json({"type": "book", "data": {
                    "token_id": tid,
                    "token_name": state.market_info.token_to_name.get(tid, ""),
                    "bids": [{"price": l.price, "size": l.size} for l in snap.bids[:30]],
                    "asks": [{"price": l.price, "size": l.size} for l in snap.asks[:30]],
                    "best_bid": snap.best_bid,
                    "best_ask": snap.best_ask,
                    "mid_price": snap.mid_price,
                    "spread": snap.spread,
                }})

            snapshots = state.get_book_snapshots()
            await ws.send_json({"type": "position", "data": state.position.to_dict(snapshots)})

            # Send wallet data
            await ws.send_json({"type": "wallet_update", "data": {
                "summary": state.wallet_tracker.get_summary(),
                "top_wallets": state.wallet_tracker.get_top_wallets(20),
                "watched": state.wallet_tracker.get_watched_wallets(),
                "recent_trades": state.wallet_tracker.get_recent_trades(20),
            }})

        try:
            while True:
                data = await ws.receive_text()
                if data == "ping":
                    await ws.send_text('{"type":"pong"}')
        except WebSocketDisconnect:
            pass
        finally:
            state.ws_clients.discard(ws)
            log.info("UI client disconnected (%d remaining)", len(state.ws_clients))

    return app
