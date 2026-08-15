"""Modal deployment artifact for the localmodal Copilot Chat provider."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

import modal

APP_NAME = os.environ.get("LOCALMODAL_APP_NAME", "localmodal-qwen")
MODEL_ID = os.environ.get("LOCALMODAL_MODEL_ID", "Qwen/Qwen3.8-27B")
MODEL_REVISION = os.environ.get(
    "LOCALMODAL_MODEL_REVISION",
    "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
)
GPU = os.environ.get("LOCALMODAL_GPU", "RTX-PRO-6000")
VLLM_PORT = 8000
MAX_MODEL_LEN = int(os.environ.get("LOCALMODAL_MAX_MODEL_LEN", "131072"))
STARTUP_TIMEOUT = 20 * 60
SCALEDOWN_WINDOW = 15 * 60
VLLM_VERSION = "0.21.0"
TRANSFORMERS_VERSION = "5.8.0"

app = modal.App(APP_NAME)

vllm_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12"
    )
    .entrypoint([])
    .uv_pip_install(
        f"vllm=={VLLM_VERSION}",
        f"transformers=={TRANSFORMERS_VERSION}",
    )
    .env(
        {
            "HF_XET_HIGH_PERFORMANCE": "1",
            "VLLM_LOG_STATS_INTERVAL": "1",
        }
    )
)

hf_cache = modal.Volume.from_name(
    f"{APP_NAME}-huggingface-cache", create_if_missing=True
)
vllm_cache = modal.Volume.from_name(
    f"{APP_NAME}-vllm-cache", create_if_missing=True
)


@app.function(
    image=vllm_image,
    gpu=GPU,
    volumes={
        "/root/.cache/huggingface": hf_cache,
        "/root/.cache/vllm": vllm_cache,
    },
    timeout=STARTUP_TIMEOUT,
    scaledown_window=SCALEDOWN_WINDOW,
    max_containers=1,
)
@modal.web_server(
    port=VLLM_PORT,
    startup_timeout=STARTUP_TIMEOUT,
    requires_proxy_auth=True,
)
def qwen_server() -> None:
    command = [
        "vllm",
        "serve",
        MODEL_ID,
        "--revision",
        MODEL_REVISION,
        "--served-model-name",
        MODEL_ID,
        "--host",
        "0.0.0.0",
        "--port",
        str(VLLM_PORT),
        "--tensor-parallel-size",
        "1",
        "--max-model-len",
        str(MAX_MODEL_LEN),
        "--enforce-eager",
        "--enable-auto-tool-choice",
        "--reasoning-parser",
        "qwen3",
        "--tool-call-parser",
        "qwen3_coder",
    ]
    print("Starting:", " ".join(command), flush=True)
    subprocess.Popen(command)


@app.local_entrypoint()
async def smoke(
    prompt: str = "Reply with exactly one short sentence proving the model is reachable.",
) -> None:
    """Warm the endpoint and stream one authenticated Chat Completions response."""
    from openai import APIConnectionError, APIStatusError, AsyncOpenAI

    token = os.environ.get("MODAL_PROXY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MODAL_PROXY_TOKEN must contain the combined Modal proxy token.")

    url = qwen_server.get_web_url()
    if not url:
        raise RuntimeError("Modal did not provide a web URL for qwen_server")
    print(f"Endpoint: {url}")
    deadline = time.monotonic() + STARTUP_TIMEOUT

    async with AsyncOpenAI(
        api_key=token,
        base_url=f"{url}/v1",
        timeout=60.0,
        max_retries=0,
    ) as client:
        while time.monotonic() < deadline:
            try:
                await client.models.list()
                break
            except APIConnectionError:
                await asyncio.sleep(5)
            except APIStatusError as exc:
                if exc.status_code not in {502, 503, 504}:
                    raise
                await asyncio.sleep(5)
        else:
            raise TimeoutError("The Qwen endpoint did not become ready before the deadline")

        stream = await client.chat.completions.create(
            model=MODEL_ID,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            max_tokens=256,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = (
                getattr(delta, "content", None)
                or getattr(delta, "reasoning", None)
                or getattr(delta, "reasoning_content", None)
            )
            if text:
                print(text, end="", flush=True)
    print()