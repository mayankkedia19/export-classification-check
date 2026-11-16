"""Stub LLM provider as a standalone HTTP service (D6): OpenAI-compatible, fixed deterministic behaviour after a
realistic delay, so load tests measure this system and not a provider's rate limits."""
import asyncio
import random
import time

from fastapi import FastAPI, HTTPException, Request

from app.cache import cache_get_json
from app.config import get_settings
from app.stubllm.policy import respond

app = FastAPI(title="stub-llm")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    s = get_settings()
    body = await request.json()
    fault = (cache_get_json("chaos:llm") or {}).get("mode")
    if fault == "provider_down":
        raise HTTPException(503, "stub provider is down (fault injection)")
    delay = (s.stub_delay_ms + random.uniform(-s.stub_jitter_ms, s.stub_jitter_ms)) / 1000
    if fault == "slow":
        delay += 30
    await asyncio.sleep(max(0.0, delay))
    text, pt, ct = respond(body["messages"])
    return {"id": "stub", "object": "chat.completion", "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}
