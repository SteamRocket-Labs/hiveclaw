# Document Conversion and Multimodal Capability Design

> Status: design draft, 2026-06-15.
> Scope: OCR, document transcription, native multimodal input, media generation, and speech model integration for Hive agents.

## Executive Summary

Hive should use Markdown as the canonical document transcription format. The goal is not just "OCR support"; the platform needs one governed document conversion layer that can ingest PDFs, Office files, images, audio, and eventually video, then expose structured Markdown plus provenance to agents.

Current code already has partial capabilities:

- Chat image upload can inline `image_data_url` for vision models.
- Runtime can transform `[image_data:data:image/...;base64,...]` markers into provider image parts for vision-capable turns.
- `read_file` can return workspace images as typed tool-result blocks.
- `read_document` and upload extraction can read common Office/PDF formats through `pdfplumber`, `pypdf`, `python-docx`, `openpyxl`, and `python-pptx`.

But this is not a complete OCR or multimodal system. OCR for scanned PDFs/images is weak or absent, document extraction logic is duplicated, large outputs are truncated without durable conversion artifacts, model capability metadata is only a `supports_vision` boolean, and image/video generation plus speech models are not first-class runtime services.

The recommended direction is:

1. Introduce a unified `DocumentConversionService` whose canonical output is Markdown.
2. Add `markitdown` as the default local converter for supported files, with guarded local-only execution.
3. Add provider-backed OCR/layout routes for higher-quality scanned documents, starting with Azure Document Intelligence or Azure Content Understanding where configured.
4. Replace ad hoc base64 marker protocols with typed attachment blocks across upload, chat runtime, tools, and provider adapters.
5. Introduce first-class model capabilities for image generation, video generation, speech-to-text, text-to-speech, and realtime voice instead of stretching `supports_vision`.

## Current State

### Document Extraction

Current extraction is split across multiple paths:

| Surface | Current path | Behavior | Gap |
|---|---|---|---|
| Chat upload | `backend/app/api/upload.py` | Saves files, extracts text inline, returns up to a short text preview. | Duplicates extraction logic, no durable Markdown artifact, no real OCR route. |
| Shared extractor | `backend/app/services/text_extractor.py` | Extracts PDF/DOCX/XLSX/PPTX text and writes companion `.txt`. | Separate implementation from `read_document`, limited layout fidelity. |
| Agent tool | `backend/app/services/agent_tool_domains/workspace.py::_read_document` | Reads PDF/DOCX/XLSX/PPTX/TXT/MD/CSV with direct libraries. | Default cap is short, scanned PDFs return empty/failure, no cache/provenance. |
| Feishu/Web fetch helpers | `backend/app/services/agent_tool_domains/feishu_drive.py`, `web_mcp.py` | Reuses the shared extractor in some paths. | Inherits weak OCR and extraction variance. |

Dependencies already present in `backend/pyproject.toml` include `pdfplumber`, `python-docx`, `openpyxl`, `python-pptx`, `reportlab`, `pypdf`, and `XlsxWriter`. `markitdown` is referenced in `docs/SKILLS_AND_PACKS_V2.md`, but it is not currently a backend dependency or runtime service.

### Native Vision Input

The web chat path has a working but narrow image channel:

- `frontend/src/pages/AgentDetail.tsx` receives `image_data_url` from upload and injects `[image_data:...]` into the prompt when the selected model has `supports_vision`.
- `backend/app/runtime/invoker.py::_apply_vision_transform` turns those markers into `LLMMessage.content` image parts.
- `backend/app/services/llm_client.py` maps those parts for OpenAI Responses and Gemini.
- `backend/app/services/agent_tool_domains/workspace.py::_read_file` returns image files as `ToolContentEnvelope.image`.
- `backend/app/services/llm_client.py` maps typed tool-result images natively for Anthropic tool results and flattens them for providers whose tool-result channel is text-only.

The gap is that this is marker-based, image-only, and uneven by provider path. The same platform concept should cover user-uploaded images, workspace image reads, document page images, audio files, and video files.

### Model Configuration

The current `llm_models` table has one multimodal field:

```text
supports_vision: bool
```

This is enough for "can the chat model see images?" but not enough for:

- OCR model selection.
- Image generation.
- Image editing.
- Video generation.
- Video understanding.
- Audio transcription.
- Speech synthesis.
- Realtime voice.
- Provider-specific file input behavior.

### Audio and Video

Existing channel code can receive some media:

- WeChat personal voice may arrive with upstream `voice_text`; Hive uses that text if present.
- WeChat personal video is saved to workspace and described as a file path.
- Telegram and channel delivery code can route media files in some outbound cases.

There is no central speech-to-text service, no text-to-speech service, no video understanding service, and no generation job service.

## Design Principles

1. Markdown is the canonical transcription surface.
   Agents should see structured Markdown, not format-specific library dumps.

2. Preserve source provenance.
   Every conversion result must know source path, content hash, engine, warnings, page count, output artifact paths, and whether OCR or visual fallback was used.

3. Avoid prompt-only ingestion.
   Large conversions should be saved as workspace artifacts and read incrementally. Upload previews can be short, but the full Markdown must be addressable.

4. Keep intelligence in the model where needed.
   Layout judgment, image OCR fallback, chart interpretation, and document understanding can use LLM vision when deterministic extraction fails or is insufficient.

5. Keep governance above capability.
   MarkItDown and cloud OCR must run under explicit file, path, URL, size, network, credential, cost, audit, and tenant boundaries.

6. Model equality.
   The canonical internal representation should be provider-neutral. Provider adapters should map to the best available native API and degrade observably when a provider lacks a modality.

## Proposed Architecture

### 1. DocumentConversionService

Add a service responsible for all file-to-Markdown conversion:

```python
@dataclass(frozen=True)
class DocumentConversionRequest:
    source_path: Path
    workspace_root: Path
    tenant_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    user_id: uuid.UUID | None
    mode: Literal["auto", "fast", "ocr", "layout", "vision"]
    max_pages: int | None = None
    max_output_chars: int | None = None
    force_refresh: bool = False


@dataclass(frozen=True)
class DocumentConversionResult:
    markdown: str
    plain_text: str
    source_path: str
    source_sha256: str
    source_mime_type: str
    engine: str
    used_ocr: bool
    used_vision: bool
    page_count: int | None
    artifact_markdown_path: str
    artifact_metadata_path: str
    warnings: tuple[str, ...]
```

Target file:

```text
backend/app/services/document_conversion.py
```

All existing document readers should route through this service:

- `backend/app/api/upload.py`
- `backend/app/services/text_extractor.py`
- `backend/app/services/agent_tool_domains/workspace.py::_read_document`
- `backend/app/services/agent_tool_domains/feishu_drive.py`
- `backend/app/services/agent_tool_domains/web_mcp.py`
- Any future Gmail/Drive/Office pack readers.

### 2. Conversion Engine Routing

The service should use ordered engines:

| Engine | Use case | Notes |
|---|---|---|
| `local_markitdown` | Default local conversion for PDF, DOCX, PPTX, XLSX, HTML, images, audio where supported. | Use local file/stream APIs only. Do not allow arbitrary remote URLs. |
| `azure_document_intelligence` | Scanned PDFs, image-heavy PDFs, layout-heavy documents, tables, handwriting. | Tenant-configured, billable, audited. |
| `azure_content_understanding` | Documents, images, audio, and video where one endpoint should route multiple modalities. | Best fit for multimodal extraction and structured field extraction. |
| `vision_ocr` | Fallback for individual images/pages/charts when local extraction is empty or low confidence. | Uses a tenant-approved vision model. |
| `legacy_python_extractors` | Fallback when MarkItDown dependency is unavailable or unsupported. | Observable fallback, not the primary long-term path. |

Routing policy:

```text
if mode == "fast":
  use local_markitdown, then legacy fallback
if mode == "ocr":
  use configured OCR provider, then vision_ocr fallback
if mode == "layout":
  prefer Document Intelligence or Content Understanding
if mode == "vision":
  render pages/images and ask a vision model
if mode == "auto":
  local_markitdown first, then detect empty/low-quality output, then escalate
```

Escalation signals:

- Extracted text is empty or below a minimum character threshold.
- PDF pages contain images but no text.
- Tables are detected but flattened poorly.
- User explicitly asks for OCR, screenshots, handwriting, forms, receipts, invoices, stamps, charts, or scans.
- File type is image/audio/video.

### 3. Artifact Storage

Each conversion should produce durable workspace artifacts:

```text
workspace/.hive/document_conversions/{source_sha256}/content.md
workspace/.hive/document_conversions/{source_sha256}/metadata.json
workspace/.hive/document_conversions/{source_sha256}/pages/page-0001.md
workspace/.hive/document_conversions/{source_sha256}/images/
```

`read_document` should return a concise preview plus artifact paths:

```text
Converted with local_markitdown.
Full Markdown: workspace/.hive/document_conversions/{sha}/content.md
Metadata: workspace/.hive/document_conversions/{sha}/metadata.json

Preview:
...
```

This avoids stuffing full long documents into a single tool result while still preserving full model-visible content through follow-up `read_file` calls.

### 4. Upload Integration

`/chat/upload` should stop owning document parsing. It should:

1. Save the uploaded file under `workspace/uploads/`.
2. For images, create a typed attachment record or data URL preview only when safe.
3. For documents, call `DocumentConversionService.convert(...)`.
4. Return:

```json
{
  "filename": "report.pdf",
  "workspace_path": "workspace/uploads/report.pdf",
  "conversion": {
    "status": "converted",
    "markdown_path": "workspace/.hive/document_conversions/abc/content.md",
    "metadata_path": "workspace/.hive/document_conversions/abc/metadata.json",
    "engine": "local_markitdown",
    "used_ocr": false,
    "warnings": []
  },
  "preview_text": "..."
}
```

The frontend should include file location and conversion artifact location in the agent prompt. It should not paste huge document bodies into the first user turn.

### 5. Tool Surface

Keep `read_document` as the primary user-facing tool because existing skills already reference it. Add optional arguments:

```json
{
  "path": "workspace/uploads/report.pdf",
  "mode": "auto",
  "max_pages": 20,
  "force_refresh": false,
  "return_format": "preview"
}
```

Possible values for `return_format`:

- `preview`: concise result plus artifact paths.
- `markdown`: full Markdown when small enough.
- `metadata`: conversion metadata only.
- `pages`: list page artifact paths.

Add `convert_document` only if tool clarity requires a separate action. If added, `read_document` should call the same service.

### 6. Typed Multimodal Attachments

Replace marker strings as the canonical internal protocol with typed blocks:

```python
@dataclass(frozen=True)
class AttachmentBlock:
    type: Literal["image", "document", "audio", "video"]
    media_type: str
    source_path: str
    data_base64: str | None = None
    artifact_path: str | None = None
    text_preview: str | None = None
```

Prompt text may still include compatibility markers temporarily, but the runtime should carry typed data through:

```text
frontend upload -> backend upload response -> chat run input -> RuntimeTask metadata -> AgentInvocationRequest -> LLMMessage content -> provider adapter
```

Provider adapter expectations:

| Provider path | User image input | Tool image result | Document input | Audio/video |
|---|---|---|---|---|
| OpenAI Responses | Native image input. | Prefer native if available, otherwise observable text fallback. | File/document input where available, otherwise Markdown artifact. | Dedicated audio/video APIs or job services. |
| Anthropic | Convert internal image block to Anthropic base64 image block. | Already has native tool-result mapping for image/document blocks. | Prefer PDF/document block if supported by model/API, otherwise Markdown artifact. | Separate service. |
| Gemini | Inline image data for native generateContent. | Current tool-result fallback should be improved when possible. | Markdown artifact or native file API when introduced. | Native media where configured, otherwise separate service. |
| OpenAI-compatible unknown | Best-effort text plus observable omitted-block warning. | Text fallback. | Markdown artifact. | Not assumed. |

### 7. Media Generation and Speech Services

Do not overload chat model invocation for generation and speech. Add separate services with explicit provider adapters:

```text
backend/app/services/media_generation/
  __init__.py
  schemas.py
  service.py
  providers/openai_images.py
  providers/openai_video.py

backend/app/services/audio/
  __init__.py
  schemas.py
  service.py
  providers/openai_speech.py
```

Tools:

| Tool | Runtime shape | Output |
|---|---|---|
| `generate_image` | Synchronous for small image jobs, async when provider requires polling. | Workspace image artifact plus metadata. |
| `edit_image` | Requires source image path plus prompt. | Workspace image artifact plus metadata. |
| `generate_video` | Always async `RuntimeTask` with polling/cancel. | Workspace video artifact plus metadata. |
| `transcribe_audio` | Sync or async based on size. | Markdown transcript plus segments metadata. |
| `synthesize_speech` | Sync for short text. | Workspace audio artifact plus metadata. |

Generation output should never be only an inline base64 string in chat. It should be an artifact under workspace and optionally sent through `send_channel_file`.

## Model Capability Schema

The model pool needs capability-level metadata. Two viable designs:

### Option A: Extend `llm_models`

Add fields:

```text
input_modalities: jsonb
output_modalities: jsonb
capabilities: jsonb
```

Example:

```json
{
  "input_modalities": ["text", "image", "document"],
  "output_modalities": ["text"],
  "capabilities": {
    "chat": true,
    "vision": true,
    "ocr": true,
    "image_generation": false,
    "image_edit": false,
    "video_generation": false,
    "speech_to_text": false,
    "text_to_speech": false,
    "realtime_voice": false
  }
}
```

### Option B: Add a model capabilities table

Add:

```text
model_capabilities
  id
  tenant_id
  model_id
  capability
  input_modalities
  output_modalities
  provider_options
  enabled
```

Recommendation: use Option A first if the platform only needs model selection. Use Option B if different capabilities need distinct quotas, credentials, or governance policies. Given Hive's control-plane direction, Option B is cleaner long term, but Option A is the lower-risk migration.

Backward compatibility:

- Continue reading `supports_vision`.
- Derive `vision=true` from `supports_vision` during migration.
- New UI should display capabilities, not only `Vision`.

## Security and Governance

### MarkItDown Guardrails

MarkItDown must not run as an unrestricted fetcher in server paths. Required rules:

- Only convert files already saved inside the agent workspace or vetted upload temp directory.
- Prefer local file or stream conversion APIs.
- Do not pass user-supplied remote URLs to permissive conversion methods.
- Reject path traversal and symlinks that escape workspace.
- Enforce file size, page count, output size, and timeout limits.
- Strip or quarantine active content where relevant.
- Log engine, source hash, tenant, agent, user, output artifact paths, and warnings.

### Cloud OCR Guardrails

Azure Document Intelligence, Azure Content Understanding, or any equivalent provider must be tenant-configured and audited:

- Tenant-scoped credentials only.
- Explicit capability policy for billable external OCR.
- Per-file and per-day budget gates.
- Provider request metadata recorded in invocation spans or a document conversion audit surface.
- Sensitive files should follow the same action-preflight principles used for external-visible or sensitive tools.

### Generated Media Guardrails

Generation tools need:

- Workspace artifact provenance.
- Prompt and source media metadata.
- Provider, model, cost, duration, and status.
- Safety errors surfaced as structured tool errors.
- Optional human checkpoint for external posting or sending to channels.

## Implementation Plan

This should be delivered as complete vertical slices, not partial hidden plumbing.

### Slice 1: Document Conversion Foundation

Complete scope:

- Add `DocumentConversionService`.
- Add `markitdown` dependency and local engine wrapper.
- Add conversion artifact storage and metadata.
- Route `read_document` through the service.
- Route `/chat/upload` document extraction through the service.
- Keep legacy extractors as observable fallback.
- Add tests for PDF/DOCX/XLSX/PPTX/text/image routing, cache reuse, path escape rejection, missing dependency fallback, scanned/empty PDF escalation decision, and artifact output.

Verification command:

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_document_conversion_service.py \
  tests/tools/test_read_document_conversion.py \
  tests/api/test_chat_upload_conversion.py \
  -q
```

### Slice 2: Native Multimodal Attachment Contract

Complete scope:

- Add typed attachment/block schema.
- Carry attachments through chat run metadata.
- Replace prompt-only `image_data` as the primary internal protocol.
- Preserve compatibility with existing `[image_data:...]` messages.
- Add provider adapter tests for OpenAI Responses, Anthropic, Gemini, and unknown OpenAI-compatible providers.
- Add upload and `read_file` tests proving images remain visible to vision-capable models.

Verification command:

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/runtime/test_multimodal_parts.py \
  tests/services/test_llm_client_multimodal.py \
  tests/tools/test_tool_content_envelope.py \
  -q
```

### Slice 3: Model Capability Surface

Complete scope:

- Add model capability schema and migration.
- Backfill from `supports_vision`.
- Update API schemas and admin UI.
- Update runtime model selection to ask for capabilities instead of a boolean.
- Keep existing agents working.

Verification command:

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/api/test_llm_model_capabilities.py \
  tests/runtime/test_context_budget.py \
  tests/runtime/test_invoker.py \
  -q
```

Frontend verification:

```bash
cd frontend
npm run build
```

### Slice 4: Audio and Media Generation Tools

Complete scope:

- Add media generation and audio service adapters.
- Add tools for image generation/editing, video generation, transcription, and speech synthesis.
- Store all outputs as workspace artifacts.
- Use `RuntimeTask` for long video jobs.
- Add governance mappings and capability gates.
- Add channel delivery integration for generated files.

Verification command:

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_media_generation_service.py \
  tests/services/test_audio_service.py \
  tests/tools/test_media_generation_tools.py \
  tests/services/test_capability_gate_policy_surface.py \
  -q
```

## Open Decisions

1. OCR provider priority:
   Recommendation: local MarkItDown first, Azure Document Intelligence for scanned/layout documents, Azure Content Understanding for multimodal audio/video extraction when credentials exist.

2. Model capability schema:
   Recommendation: use JSON capability fields first only if speed matters. Prefer a dedicated capability table if quotas, credentials, and governance need per-capability control.

3. Video understanding:
   Recommendation: do not implement as frame sampling only. Use provider-native video understanding where available, with frame extraction as observable fallback.

4. Audio source of truth:
   Recommendation: store transcript Markdown and segment metadata as artifacts. Channel-provided transcriptions such as WeChat `voice_text` can be accepted as a low-cost preview, but not as the only transcript path.

5. MarkItDown plugin usage:
   Recommendation: keep plugins disabled by default in hosted runtime. Enable only explicitly allowed plugins after security review.

## Acceptance Criteria

The capability should be considered complete only when all of the following are true:

- A scanned PDF uploaded in chat produces a readable Markdown artifact or a clear structured conversion error.
- A native text PDF, DOCX, PPTX, and XLSX all route through the same conversion service.
- `read_document` returns artifact paths and can reuse cached conversion output.
- Upload preview no longer relies on duplicated extraction logic.
- Vision-capable chat models can see uploaded images through typed attachments.
- Text-only providers receive explicit observable fallback notes when non-text blocks are omitted.
- Admin model configuration can represent at least chat, vision, OCR, image generation, video generation, speech-to-text, and text-to-speech capabilities.
- Generated images/audio/video are saved as workspace artifacts with provenance.
- All external OCR/generation calls are tenant-scoped, audited, and capability-gated.

