# Deploying the OCR Extractor on OpenShift / OpenShift AI

## Overview

This guide migrates the local `extract_image_full_ocr.py` application — which ran against a local Ollama instance on Windows — to run on OpenShift, calling the Granite Docling VLM endpoint already deployed and tested on OpenShift AI:

```text
https://granite-docling-vlm-predictor-docling-maas.apps.ocp4.example.com
```

The script is packaged as a container image and deployed either as a **Kubernetes Job** (for one-shot or batch document processing) or as an **OpenShift AI Workbench** (for interactive use).

---

## What Was Modified in the Script

The following changes were made to `extract_image_full_ocr.py` to support OpenShift deployment without breaking the existing Ollama workflow:

| Change | Why |
|---|---|
| Added `--no-verify-ssl` flag | Lab cluster uses self-signed TLS certificates |
| `DEFAULT_VLLM_MODEL = "granite-docling-258m"` | Matches the `--served-model-name` in the KServe ServingRuntime |
| All key flags read from `DOCLING_*` env vars | Allows OpenShift ConfigMaps/Secrets to configure the container without rewriting the pod command |
| SSL bypass applied at three levels (env, ssl context, monkey-patch) | Required because docling depends on multiple HTTP clients (urllib, requests, httpx) |

The original Ollama workflow is unchanged. `--runtime ollama` still works exactly as before.

---

## Architecture on OpenShift

```
┌─────────────────────────────────────────────┐
│  OpenShift Cluster                          │
│                                             │
│  ┌─────────────────┐   HTTPS    ┌────────────────────────────┐
│  │  ocr-extractor  │──────────▶ │  granite-docling-vlm       │
│  │  Job / Pod      │            │  KServe InferenceService   │
│  │  (docling-maas  │            │  (docling-maas namespace)  │
│  │   namespace or  │            │  vLLM CPU ServingRuntime   │
│  │   any namespace)│            └────────────────────────────┘
│  └────────┬────────┘
│           │ mounts
│  ┌────────▼────────┐
│  │  PVC: ocr-data  │  ← input images copied here
│  │  /app/input     │  ← extracted-markdown written here
│  │  /app/out       │
│  └─────────────────┘
└─────────────────────────────────────────────┘
```

When both the extractor pod and the predictor are in the same cluster, the call travels over the internal cluster network — no internet egress required.

---

## Prerequisites

- `oc` CLI configured and logged in to the cluster.
- Docker or Podman installed on the build machine.
- Access to an image registry reachable from the cluster. This guide uses the **OpenShift internal registry** (`image-registry.openshift-image-registry.svc:5000`).
- The Granite Docling endpoint tested and reachable (confirmed working).

---

## Phase 0 — Pre-Flight Checklist

Run these checks before starting the build. Each command should return a clean result. Fix every failure before moving to Phase 1.

### 0.1 Confirm `oc` Login and Target Namespace

```bash
# Must return your username, not an error
oc whoami

# Confirm you are in the correct namespace
oc project docling-maas
```

### 0.2 Confirm the Predictor Pod Is Running

```bash
oc get pods -n docling-maas
```

Expected: one or more `granite-docling-vlm-predictor-*` pods with **`Running`** status and `2/2` ready containers. If the pod is in `Pending` or `CrashLoopBackOff`, fix the predictor before deploying the extractor.

### 0.3 Smoke-Test the Predictor Endpoint (HTTP 200)

```bash
# Should return: HTTP 200
curl -k -s -o /dev/null -w "HTTP %{http_code}\n" \
  "https://granite-docling-vlm-predictor-docling-maas.apps.ocp4.example.com/v1/models"
```

Any response other than `HTTP 200` means the endpoint is not ready — stop here and check the predictor.

### 0.4 Verify the Served Model Name

The model name used in the ConfigMap (`DOCLING_MODEL`) must exactly match the name vLLM reports:

```bash
curl -k -s \
  "https://granite-docling-vlm-predictor-docling-maas.apps.ocp4.example.com/v1/models" \
  | python3 -c \
    "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

Expected output: `granite-docling-258m`. If the name differs, update `DOCLING_MODEL` in the ConfigMap you create in Phase 2.2 before submitting any job.

### 0.5 Confirm Required Local Files Exist

```bash
ls -1 extract_image_full_ocr.py requirements.txt Dockerfile
```

All three must be present in the current directory. Missing files will cause the build in Phase 1 to fail.

### 0.6 Confirm Container Build Tool Is Available

```bash
podman --version || docker --version
```

If neither tool is available on your workstation, use the OpenShift BuildConfig alternative described in Phase 1.4.

---

## Phase 1 — Build and Push the Container Image

### 1.1 Confirm Required Files Exist

After the modifications, your project directory should contain:

```text
extract_image_full_ocr.py   ← modified script
requirements.txt
Dockerfile
```

### 1.2 Log in to the OpenShift Internal Registry

```bash
# Expose the internal registry externally if not already done (one-time cluster admin task)
oc patch configs.imageregistry.operator.openshift.io/cluster \
  --type merge -p '{"spec":{"defaultRoute":true}}'

# Get the external registry hostname
REGISTRY=$(oc get route default-route -n openshift-image-registry \
  -o jsonpath='{.spec.host}')
echo $REGISTRY

# Log in using your OpenShift token
podman login --tls-verify=false -u $(oc whoami) \
  -p $(oc whoami --show-token) $REGISTRY
```

### 1.3 Build the Image Locally and Push It

```bash
# Build
IMAGE=$REGISTRY/docling-maas/ocr-extractor:latest
podman build -t $IMAGE .

# Push
podman push --tls-verify=false $IMAGE
```

> [!NOTE]
> The `docling` package downloads model assets and Hugging Face tokenizer files on first use,
> not at build time. The first pod start after an image pull will be slower than subsequent runs.
> If your cluster has no internet access, see Phase 5 (offline tips).

### 1.4 (Alternative) Build Directly on OpenShift with a BuildConfig

If you cannot build locally, push the source to OpenShift and let it build there:

```bash
# Create a new project / use the existing one
oc project docling-maas

# Create a BuildConfig from the local directory
oc new-build --name=ocr-extractor \
  --binary=true \
  --strategy=docker \
  -n docling-maas

# Start the build by uploading the local directory as the build context
oc start-build ocr-extractor --from-dir=. --follow -n docling-maas

# The image is stored in the internal registry automatically
# imagestream: ocr-extractor:latest
```

### 1.5 Validate the Image Is Available in the Registry

Run this after either the local push (1.3) or the BuildConfig build (1.4):

```bash
# Confirm the ImageStream tag exists and shows a valid image digest
oc get istag ocr-extractor:latest -n docling-maas
```

Expected: output row with a SHA256 digest in the `IMAGE REFERENCE` column. An error such as `not found` means the push or build did not complete successfully — do not proceed to Phase 2.

Optional: do a quick dry-run to confirm the image is pullable and the script starts correctly:

```bash
oc run img-check -n docling-maas --restart=Never --rm -i \
  --image=image-registry.openshift-image-registry.svc:5000/docling-maas/ocr-extractor:latest \
  -- python extract_image_full_ocr.py --help
```

Expected: the script usage text printed, pod exits `0` and auto-deletes.

---

## Phase 2 — Create OpenShift Resources

All YAML blocks below can be saved to a file (e.g. `ocr-job.yaml`) and applied with `oc apply -f ocr-job.yaml`.

### 2.1 Create the Project (if not already using `docling-maas`)

```bash
oc new-project ocr-extractor
# Or reuse the existing project:
oc project docling-maas
```

### 2.2 ConfigMap — Endpoint Configuration

This ConfigMap holds all DOCLING_* configuration values. Change only this resource when targeting a different endpoint or model — no image rebuild required.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ocr-extractor-config
  namespace: docling-maas
data:
  DOCLING_RUNTIME: "vllm"
  DOCLING_ENDPOINT: "https://granite-docling-vlm-predictor-docling-maas.apps.ocp4.example.com/v1/chat/completions"
  DOCLING_MODEL: "granite-docling-258m"
  DOCLING_TIMEOUT: "180"
  DOCLING_MAX_TOKENS: "4096"
  DOCLING_NO_VERIFY_SSL: "true"
```

Apply it:

```bash
oc apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: ocr-extractor-config
  namespace: docling-maas
data:
  DOCLING_RUNTIME: "vllm"
  DOCLING_ENDPOINT: "https://granite-docling-vlm-predictor-docling-maas.apps.ocp4.example.com/v1/chat/completions"
  DOCLING_MODEL: "granite-docling-258m"
  DOCLING_TIMEOUT: "180"
  DOCLING_MAX_TOKENS: "4096"
  DOCLING_NO_VERIFY_SSL: "true"
EOF
```

> [!IMPORTANT]
> `DOCLING_NO_VERIFY_SSL: "true"` is set because this lab cluster uses self-signed TLS certificates.
> Remove this key in environments with properly signed certificates.

### 2.3 PersistentVolumeClaim — Shared Input/Output Storage

The Job reads input images from `/app/input` and writes Markdown output to `/app/out`. A PVC provides durable storage between job runs.

```bash
oc apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ocr-data
  namespace: docling-maas
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 2Gi
EOF
```

### 2.4 Copy Input Images Into the PVC

Use a temporary pod to copy images from your workstation into the PVC:

```bash
# Start a helper pod that mounts the PVC
oc run pvc-helper --restart=Never \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
  --overrides='{"spec":{"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"ocr-data"}}],"containers":[{"name":"pvc-helper","image":"registry.access.redhat.com/ubi9/ubi-minimal:latest","command":["sleep","3600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}]}}' \
  -n docling-maas

# Wait for it to be running
oc wait pod/pvc-helper -n docling-maas --for=condition=Ready --timeout=60s

# Copy your image file(s) into the PVC
oc cp page-1.png docling-maas/pvc-helper:/data/input/page-1.png

# Confirm
oc exec pvc-helper -n docling-maas -- ls -lh /data/input/

# Clean up the helper pod when done
oc delete pod pvc-helper -n docling-maas
```

### 2.5 Validate All Phase 2 Resources Before Proceeding

One quick pass to confirm every resource is in place before submitting the Job:

```bash
# 1. ConfigMap exists and has the expected keys
oc get configmap ocr-extractor-config -n docling-maas \
  -o jsonpath='{range .data.*}{@}{"\n"}{end}'
```

Expected: six lines of values — the runtime, endpoint URL, model name, timeout, max-tokens, and SSL flag. If any are missing or wrong, correct the ConfigMap before continuing.

```bash
# 2. PVC is Bound (not Pending)
oc get pvc ocr-data -n docling-maas
```

Expected: `STATUS = Bound`. `Pending` means no storage class could satisfy the claim — check available StorageClasses: `oc get storageclass`.

```bash
# 3. Confirm the input file is present and non-zero in the PVC
# (If the pvc-helper pod was deleted, recreate it first — see Phase 2.4)
oc exec pvc-helper -n docling-maas -- ls -lh /data/input/
```

Expected: at least one file with a non-zero size. An empty listing means the `oc cp` in step 2.4 did not complete — re-run it.

---

## Phase 3 — Run the Extractor as a Kubernetes Job

### 3.1 Single-Image Job

Replace `page-1.png` with the actual filename you copied into the PVC.

```bash
oc apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: ocr-extract-page1
  namespace: docling-maas
spec:
  backoffLimit: 1
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: ocr-extractor
          image: image-registry.openshift-image-registry.svc:5000/docling-maas/ocr-extractor:latest
          imagePullPolicy: Always
          args:
            - "/app/input/page-1.png"
            - "--output"
            - "/app/out/page-1.md"
          envFrom:
            - configMapRef:
                name: ocr-extractor-config
          resources:
            requests:
              cpu: "1"
              memory: 2Gi
            limits:
              cpu: "2"
              memory: 4Gi
          volumeMounts:
            - name: data
              mountPath: /app/input
              subPath: input
            - name: data
              mountPath: /app/out
              subPath: out
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: ocr-data
EOF
```

### 3.2 Monitor the Job

```bash
# Watch job status
oc get job ocr-extract-page1 -n docling-maas -w

# If the job pod does not reach Running within ~2 minutes, check for image-pull
# or scheduling problems before waiting longer:
oc describe pod -l job-name=ocr-extract-page1 -n docling-maas

# Stream logs while the job is running
oc logs -f job/ocr-extract-page1 -n docling-maas

# Check final status
oc describe job ocr-extract-page1 -n docling-maas
```

Expected final status: `Succeeded: 1`. If `Failed: 1` appears, check logs with `oc logs job/ocr-extract-page1 -n docling-maas` (logs are retained after the pod exits).

### 3.3 Validate the Output File Before Copying

Before retrieving the output, confirm the file was actually written and is not empty:

```bash
# Start the helper pod (reuse Phase 2.4 steps if it was deleted)
oc run pvc-helper --restart=Never \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
  --overrides='{"spec":{"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"ocr-data"}}],"containers":[{"name":"pvc-helper","image":"registry.access.redhat.com/ubi9/ubi-minimal:latest","command":["sleep","3600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}]}}' \
  -n docling-maas

oc wait pod/pvc-helper -n docling-maas --for=condition=Ready --timeout=60s

# Check the output directory
oc exec pvc-helper -n docling-maas -- ls -lh /data/out/
```

Expected: `page-1.md` present with a non-zero file size (typically several KB). A zero-byte file or missing file means the model returned empty content — see the **Output Markdown Is Empty** section in Troubleshooting.

### 3.4 Retrieve the Output Markdown

```bash
# Start a helper pod again to read the output
oc run pvc-helper --restart=Never \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
  --overrides='{"spec":{"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"ocr-data"}}],"containers":[{"name":"pvc-helper","image":"registry.access.redhat.com/ubi9/ubi-minimal:latest","command":["sleep","3600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}]}}' \
  -n docling-maas

oc wait pod/pvc-helper -n docling-maas --for=condition=Ready --timeout=60s

# Copy the output to your local machine
oc cp docling-maas/pvc-helper:/data/out/page-1.md ./page-1.md

# Print it
cat page-1.md

# Clean up
oc delete pod pvc-helper -n docling-maas
```

### 3.5 Clean Up a Completed Job Before Re-Running

OpenShift prevents creating a Job with the same name if the previous one still exists:

```bash
oc delete job ocr-extract-page1 -n docling-maas
```

---

## Phase 4 — Run from an OpenShift AI Workbench (Interactive)

If you prefer interactive use rather than a Job, you can run the script directly from an OpenShift AI Workbench terminal. The Workbench pod is already inside the cluster, so the predictor endpoint is reachable without extra routing.

### 4.1 Open a Terminal in Your Workbench

Launch the `docling-maas` Workbench in the RHOAI Dashboard, then open a JupyterLab terminal.

### 4.2 Install Dependencies

```bash
pip install docling Pillow
```

### 4.3 Run Against the In-Cluster Endpoint

```bash
python extract_image_full_ocr.py page-1.png \
  --runtime vllm \
  --endpoint "https://granite-docling-vlm-predictor-docling-maas.apps.ocp4.example.com/v1/chat/completions" \
  --model granite-docling-258m \
  --timeout 180 \
  --no-verify-ssl \
  --output out/page-1.md
```

The `--no-verify-ssl` flag is required because the lab route uses a self-signed certificate. Output is written to `out/page-1.md` relative to the working directory.

---

## Phase 5 — In-Cluster Endpoint Optimization

When the extractor pod runs in the same OpenShift cluster as the predictor, you can bypass the external HAProxy route and call the Knative internal service URL directly. This eliminates the HAProxy timeout layer entirely and gives lower-latency access.

### 5.1 Find the Internal Service URL

```bash
oc get ksvc granite-docling-vlm-predictor -n docling-maas \
  -o jsonpath='{.status.address.url}'
```

The internal address looks like:
```text
http://granite-docling-vlm-predictor.docling-maas.svc.cluster.local
```

### 5.2 Update the ConfigMap to Use the Internal URL

```bash
oc patch configmap ocr-extractor-config -n docling-maas \
  --type merge \
  -p '{
    "data": {
      "DOCLING_ENDPOINT": "http://granite-docling-vlm-predictor.docling-maas.svc.cluster.local/v1/chat/completions",
      "DOCLING_NO_VERIFY_SSL": "false"
    }
  }'
```

Benefits of the internal URL:
- No TLS certificate issues — plain HTTP inside the cluster.
- No HAProxy timeout layer to fight.
- Lower latency.
- `DOCLING_NO_VERIFY_SSL` can be set to `false`.

> [!NOTE]
> The internal URL is only reachable from inside the same OpenShift cluster.
> External workstations must still use the HTTPS external route.

---

## Phase 6 — Processing Multiple Images (Batch Job Pattern)

To process an entire directory of page images, use a simple shell loop in the Job command instead of calling the Python script once:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: ocr-extract-batch
  namespace: docling-maas
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: ocr-extractor
          image: image-registry.openshift-image-registry.svc:5000/docling-maas/ocr-extractor:latest
          imagePullPolicy: Always
          command: ["/bin/bash", "-c"]
          args:
            - |
              set -e
              for img in /app/input/*.png /app/input/*.jpg; do
                [ -e "$img" ] || continue
                base=$(basename "${img%.*}")
                echo "Processing $img ..."
                python /app/extract_image_full_ocr.py "$img" \
                  --output "/app/out/${base}.md"
                echo "Done: /app/out/${base}.md"
              done
          envFrom:
            - configMapRef:
                name: ocr-extractor-config
          resources:
            requests:
              cpu: "1"
              memory: 2Gi
            limits:
              cpu: "2"
              memory: 4Gi
          volumeMounts:
            - name: data
              mountPath: /app/input
              subPath: input
            - name: data
              mountPath: /app/out
              subPath: out
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: ocr-data
```

---

## Configuration Reference

All configuration values the container reads from environment variables:

| Environment Variable | CLI Flag | Default | Description |
|---|---|---|---|
| `DOCLING_RUNTIME` | `--runtime` | `vllm` | Backend type: `ollama`, `vllm`, or `generic` |
| `DOCLING_ENDPOINT` | `--endpoint` | `http://localhost:8000/v1/chat/completions` | OpenAI-compatible chat completions URL |
| `DOCLING_MODEL` | `--model` | `granite-docling-258m` | Model name as served by vLLM |
| `DOCLING_TIMEOUT` | `--timeout` | `120` | Per-request timeout in seconds |
| `DOCLING_MAX_TOKENS` | `--max-tokens` | `8192` | Maximum generated tokens per page |
| `DOCLING_NO_VERIFY_SSL` | `--no-verify-ssl` | `false` | Set `true` for self-signed cert environments |
| `DOCLING_SKIP_HEALTH_CHECK` | `--skip-health-check` | `false` | Skip `/v1/models` probe before conversion |

CLI flags always override environment variables when explicitly provided.

---

## Troubleshooting

### Job Stays in `Pending`

```bash
oc describe pod -l job-name=ocr-extract-page1 -n docling-maas
```

Common causes:
- Image pull error — confirm the image was pushed and the stream tag exists: `oc get is ocr-extractor -n docling-maas`.
- PVC not bound — check: `oc get pvc ocr-data -n docling-maas`.
- Insufficient CPU/memory — reduce `requests` in the Job spec.

### Job Fails with `FileNotFoundError`

The input file was not copied into the PVC. Re-run the `oc cp` step in Phase 2.4 and confirm with: `oc exec pvc-helper -n docling-maas -- ls /data/input/`.

### Job Fails with `Could not reach ... /v1/models`

The health check cannot reach the predictor. Either:
- The predictor pod is not running: `oc get pods -n docling-maas`.
- The endpoint URL in the ConfigMap is wrong.
- Add `DOCLING_SKIP_HEALTH_CHECK: "true"` to the ConfigMap to bypass the probe and see the actual inference error.

### Job Fails with SSL or EOF Errors

Confirm `DOCLING_NO_VERIFY_SSL: "true"` is set in the ConfigMap and the ConfigMap is referenced in the Job's `envFrom`. Use the internal cluster endpoint (Phase 5) to eliminate TLS entirely.

### Output Markdown Is Empty

The model returned no content. Check:
- The image file is a valid, readable PNG/JPG.
- The model name matches exactly: `granite-docling-258m` (lowercase, with hyphens).
- Increase `DOCLING_MAX_TOKENS` to `8192` if it was reduced.
- Check predictor logs: `oc logs -n docling-maas deploy/granite-docling-vlm-predictor --tail=50`.
