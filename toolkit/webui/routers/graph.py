"""Typed service topology endpoints backed by the controller."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError

router = APIRouter(prefix="/api/services", tags=["services-graph"])


async def _topology(request: Request):
    try:
        return await run_in_threadpool(request.app.state.controller.service_topology)
    except ControllerClientError:
        return None


@router.get("/graph")
async def service_graph(request: Request):
    topology = await _topology(request)
    if topology is None:
        return JSONResponse({"error": "Service topology is unavailable"}, status_code=503)
    return {
        "nodes": [node.model_dump(mode="json") for node in topology.nodes],
        "edges": [edge.model_dump(mode="json") for edge in topology.edges],
    }


@router.get("/catalog")
async def service_catalog(request: Request):
    topology = await _topology(request)
    if topology is None:
        return JSONResponse({"error": "Service catalog is unavailable"}, status_code=503)
    return [entry.model_dump(mode="json") for entry in topology.catalog]
