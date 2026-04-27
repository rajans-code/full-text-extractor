# ---------------------------------------------------------------------------
# OCR Extractor — Granite Docling VLM client
# Base image: Red Hat UBI9 Python 3.11 (runs non-root as UID 1001 by default,
# which satisfies OpenShift's restricted-v2 SCC without any extra policy).
# ---------------------------------------------------------------------------
FROM registry.access.redhat.com/ubi9/python-311:latest

LABEL name="ocr-extractor" \
      description="Docling VLM client for Granite Docling inference on OpenShift AI" \
      version="1.0"

WORKDIR /app

# 1. Install Python dependencies first — Docker layer cache saves rebuild time
#    when only the application code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 2. Copy application code.
COPY extract_image_full_ocr.py .

# 3. Create the input/output directories and make them group-writable.
#    OpenShift runs containers with a random UID in the root group (GID 0),
#    so g=u permissions let the process write here without running as root.
RUN mkdir -p /app/input /app/out && \
    chmod -R g=u /app

# 4. Default environment — override in ConfigMap / Secret in OpenShift.
ENV DOCLING_RUNTIME="vllm" \
    DOCLING_ENDPOINT="https://granite-docling-vlm-predictor-docling-maas.apps.ocp4.example.com/v1/chat/completions" \
    DOCLING_MODEL="granite-docling-258m" \
    DOCLING_TIMEOUT="180" \
    DOCLING_MAX_TOKENS="4096" \
    DOCLING_NO_VERIFY_SSL="true"

# 5. Entrypoint — the input image path is supplied via the Job command or
#    the DOCLING_INPUT_IMAGE env var wrapper script.
ENTRYPOINT ["python", "extract_image_full_ocr.py"]
CMD ["--help"]
