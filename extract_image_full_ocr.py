from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from typing import Any

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import VlmPipelineOptions
from docling.datamodel.pipeline_options_vlm_model import ApiVlmOptions, ResponseFormat
from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline


DEFAULT_OLLAMA_MODEL = "ibm/granite-docling:latest"
# The HuggingFace model id — used as a documentation reference.
DEFAULT_VLLM_HF_MODEL = "ibm-granite/granite-docling-258M"
# The model name vLLM actually serves (set via --served-model-name in the ServingRuntime).
DEFAULT_VLLM_MODEL = "granite-docling-258m"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract text from an image using Docling's VLM pipeline and a "
            "remote Granite Docling endpoint."
        )
    )
    parser.add_argument(
        "image",
        type=Path,
        help="Path to an input image, for example .png, .jpg, .jpeg, or .tif.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("out/extracted.md"),
        help="Markdown output file. Default: out/extracted.md",
    )
    parser.add_argument(
        "--runtime",
        choices=("ollama", "vllm", "generic"),
        default=os.environ.get("DOCLING_RUNTIME", "ollama"),
        help="Remote runtime type. Default: ollama (env: DOCLING_RUNTIME)",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("DOCLING_ENDPOINT"),
        help=(
            "OpenAI-compatible chat completions endpoint. Defaults to "
            "http://localhost:8000/v1/chat/completions for vLLM/generic runtimes "
            "(env: DOCLING_ENDPOINT)."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DOCLING_MODEL"),
        help=(
            "Model id for generic/vLLM endpoints. Defaults to "
            f"{DEFAULT_VLLM_MODEL} for vLLM. Ollama uses Docling's Granite preset "
            f"model, usually {DEFAULT_OLLAMA_MODEL} "
            "(env: DOCLING_MODEL)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("DOCLING_TIMEOUT", "120")),
        help="Per-request timeout in seconds. Default: 120 (env: DOCLING_TIMEOUT)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.environ.get("DOCLING_MAX_TOKENS", "4096")),
        help="Maximum output tokens for generic/vLLM endpoints. Default: 4096 (env: DOCLING_MAX_TOKENS)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=2.0,
        help="Docling image scale multiplier. Default: 2.0",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=None,
        help="Optional maximum image dimension before VLM processing.",
    )
    parser.add_argument(
        "--skip-health-check",
        action="store_true",
        default=os.environ.get("DOCLING_SKIP_HEALTH_CHECK", "").lower() in ("1", "true", "yes"),
        help="Do not probe the local endpoint before conversion (env: DOCLING_SKIP_HEALTH_CHECK).",
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        default=os.environ.get("DOCLING_NO_VERIFY_SSL", "").lower() in ("1", "true", "yes"),
        help=(
            "Disable TLS certificate verification for the remote endpoint and the "
            "health-check probe. Use ONLY in lab or dev environments that have "
            "self-signed certificates. Never enable this in production "
            "(env: DOCLING_NO_VERIFY_SSL)."
        ),
    )
    return parser.parse_args()


def get_ollama_models() -> list[str]:
    request = urllib.request.Request(OLLAMA_TAGS_URL, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))

    models = []
    for item in payload.get("models", []):
        name = item.get("name") or item.get("model")
        if name:
            models.append(name)
    return models


def resolve_ollama_model(requested_model: str | None) -> str:
    models = get_ollama_models()
    granite_docling_models = [name for name in models if "granite-docling" in name]

    if requested_model:
        if requested_model in models:
            return requested_model

        installed = ", ".join(models) if models else "none"
        raise RuntimeError(
            f"Ollama model '{requested_model}' is not installed. "
            f"Installed Ollama models: {installed}."
        )

    if DEFAULT_OLLAMA_MODEL in models:
        return DEFAULT_OLLAMA_MODEL

    if "ibm/granite-docling:258m" in models:
        return "ibm/granite-docling:258m"

    if granite_docling_models:
        return granite_docling_models[0]

    installed = ", ".join(models) if models else "none"
    raise RuntimeError(
        "No Granite Docling model was found in Ollama. "
        f"Installed Ollama models: {installed}. "
        "Run: ollama pull ibm/granite-docling"
    )


def make_api_vlm_options(
    runtime: str,
    endpoint: str | None,
    model: str | None,
    timeout: float,
    max_tokens: int,
    scale: float,
    max_size: int | None,
) -> ApiVlmOptions:
    if runtime == "ollama":
        ollama_model = resolve_ollama_model(model)
        url = "http://localhost:11434/v1/chat/completions"
        params: dict[str, Any] = {
            "model": ollama_model,
            "max_tokens": max_tokens,
            "skip_special_tokens": False,
        }
    else:  # vllm or generic
        if not endpoint:
            endpoint = "http://localhost:8000/v1/chat/completions"
        url = endpoint
        params = {
            "model": model or DEFAULT_VLLM_MODEL,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "skip_special_tokens": False,
        }

    return ApiVlmOptions(
        url=url,
        params=params,
        timeout=timeout,
        prompt="Convert this page to docling.",
        response_format=ResponseFormat.DOCTAGS,
        scale=scale,
        max_size=max_size,
        temperature=0.0,
        stop_strings=["</doctag>", "<|end_of_text|>"],
    )


def build_converter(args: argparse.Namespace) -> DocumentConverter:
    vlm_options = make_api_vlm_options(
        runtime=args.runtime,
        endpoint=args.endpoint,
        model=args.model,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        scale=args.scale,
        max_size=args.max_size,
    )
    pipeline_options = VlmPipelineOptions(
        vlm_options=vlm_options,
        enable_remote_services=True,
        document_timeout=args.timeout + 30,
    )

    image_options = ImageFormatOption(
        pipeline_cls=VlmPipeline,
        pipeline_options=pipeline_options,
    )
    pdf_options = PdfFormatOption(
        pipeline_cls=VlmPipeline,
        pipeline_options=pipeline_options,
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.IMAGE, InputFormat.PDF],
        format_options={
            InputFormat.IMAGE: image_options,
            InputFormat.PDF: pdf_options,
        },
    )


def health_check(args: argparse.Namespace) -> None:
    if args.skip_health_check:
        return

    if args.runtime == "ollama":
        url = OLLAMA_TAGS_URL
    else:
        endpoint = args.endpoint or "http://localhost:8000/v1/chat/completions"
        url = endpoint.replace("/v1/chat/completions", "/v1/models")

    ssl_ctx: ssl.SSLContext | None = None
    if args.no_verify_ssl and url.startswith("https://"):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5, context=ssl_ctx) as response:
            if response.status >= 400:
                raise RuntimeError(f"Endpoint returned HTTP {response.status}: {url}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            f"Could not reach {url}. Start the Granite Docling endpoint first, "
            "or pass --skip-health-check if your endpoint does not expose this probe."
        ) from exc




def convert_image(args: argparse.Namespace) -> str:
    input_path = args.image.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    if not input_path.is_file():
        raise ValueError(f"Input path is not a file: {input_path}")

    if args.no_verify_ssl:
        # WARNING: This disables TLS verification globally for this process.
        # Intended for lab/dev environments with self-signed certificates only.
        warnings.warn(
            "--no-verify-ssl is active. TLS certificate verification is disabled. "
            "Use only in lab or dev environments.",
            stacklevel=2,
        )
        # 1. urllib / http.client
        ssl._create_default_https_context = ssl._create_unverified_context  # noqa: SLF001
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        # 2. requests / urllib3 — monkey-patch Session.send so that every
        #    outgoing request (including docling's internal API calls) skips
        #    certificate verification regardless of how the session was created.
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _orig_send = requests.Session.send
        def _unverified_send(self, request, **kwargs):  # noqa: ANN001
            kwargs["verify"] = False  # force-override, setdefault won't work here
            return _orig_send(self, request, **kwargs)
        requests.Session.send = _unverified_send

    health_check(args)
    converter = build_converter(args)
    result = converter.convert(source=input_path)
    extracted_markdown = result.document.export_to_markdown()
    if not extracted_markdown.strip():
        raise RuntimeError(
            "Docling returned empty Markdown. Check the Ollama model name and the "
            "API error output above."
        )
    return extracted_markdown


def main() -> int:
    args = parse_args()
    try:
        extracted_markdown = convert_image(args)
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(extracted_markdown, encoding="utf-8")
        print(extracted_markdown)
        print(f"\nSaved extracted Markdown to {output_path}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
