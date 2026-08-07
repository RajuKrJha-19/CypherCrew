"""Google Gemini backend (default). Uses the google-genai SDK.

Imported lazily (only when AI is on, not simulating, and AI_PROVIDER=gemini),
so the dependency is inert otherwise and this module is import-safe even when
google-genai is not installed. Vision is inline image/PDF bytes.
"""
from app.ai import prompts
from app.ai.base import AIProvider, CaptionResult, Finding
from app.ai.errors import AIAuth, AIPermanent, AITransient
from app.ai.parsing import extract_json, strip_fences, salvage_caption


def _genai():
    """Import the SDK on demand; a clean typed error if it's missing."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - only when dep absent
        raise AIPermanent(
            "google-genai is not installed; run pip install -r requirements.txt"
        ) from exc
    return genai, types


class GeminiProvider(AIProvider):
    key = "gemini"

    # -- infra --------------------------------------------------------------

    def _client(self):
        if not self.api_key:
            raise AIAuth("No Gemini API key configured.")
        genai, types = _genai()
        # Apply our timeout (ms) so a slow vision call can't hang a worker.
        # Guarded: if this SDK build doesn't accept http_options, fall back to
        # a plain client rather than breaking generation.
        try:
            http = types.HttpOptions(timeout=int(self.timeout_s * 1000))
            return genai.Client(api_key=self.api_key, http_options=http)
        except Exception:  # noqa: BLE001
            return genai.Client(api_key=self.api_key)

    def _parts(self, types, user_text, media):
        parts = [types.Part.from_text(text=user_text)]
        for m in media or []:
            parts.append(types.Part.from_bytes(data=m.data, mime_type=m.mime_type))
        return parts

    def _generate(self, model, system, user_text, media, *, as_json):
        genai, types = _genai()
        client = self._client()
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=self.max_tokens,
            response_mime_type="application/json" if as_json else "text/plain",
        )
        try:
            resp = client.models.generate_content(
                model=model,
                contents=self._parts(types, user_text, media),
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - normalized to typed errors
            raise self._map_error(exc)
        meta = getattr(resp, "usage_metadata", None)
        if meta is not None:
            self._add_usage(getattr(meta, "prompt_token_count", 0),
                           getattr(meta, "candidates_token_count", 0))
        return resp.text or ""

    @staticmethod
    def _map_error(exc):
        # Never surface the key: only the SDK's status/message, which the SDK
        # does not populate with credentials.
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        msg = f"Gemini request failed ({status})" if status else "Gemini request failed"
        if status in (401, 403):
            return AIAuth(msg)
        if status in (429, 500, 502, 503, 504) or "timeout" in str(exc).lower():
            return AITransient(msg)
        return AIPermanent(msg)

    # -- capabilities -------------------------------------------------------

    def generate_caption(self, ctx):
        system, user = prompts.caption_prompt(ctx)
        raw = self._generate(self.model, system, user, ctx.media,
                             as_json=True)
        data = extract_json(raw)
        if data is None:
            # JSON didn't parse (usually a token-limit truncation mid-object).
            # Salvage just the caption field so the box shows a CLEAN caption,
            # never the raw JSON blob. If there's no caption to salvage and the
            # reply is plain text, use that; a bare broken-JSON reply is a
            # visible error, not something dumped into the caption.
            salvaged = salvage_caption(raw)
            if salvaged:
                return CaptionResult(caption=salvaged)
            text = strip_fences(raw)
            if text and not text.lstrip().startswith("{"):
                return CaptionResult(caption=text)
            raise AIPermanent(
                "Gemini returned an unparseable caption (it may have hit the "
                "token limit) - try again or a different model.")
        hashtags = [h.lstrip("#") for h in (data.get("hashtags") or [])
                    if isinstance(h, str)]
        keywords = [k.strip() for k in (data.get("keywords") or [])
                    if isinstance(k, str) and k.strip()]
        per = {k: v for k, v in (data.get("per_platform") or {}).items()
               if isinstance(v, str)}
        variations = [v for v in (data.get("variations") or [])
                      if isinstance(v, str) and v.strip()]
        return CaptionResult(
            caption=str(data.get("caption") or ""),
            per_platform=per,
            hashtags=hashtags,
            keywords=keywords,
            first_comment=str(data.get("first_comment") or ""),
            variations=variations,
        )

    def generate_alt_text(self, image):
        system, user = prompts.alt_text_prompt(image)
        raw = self._generate(self.model, system, user, [image],
                             as_json=False)
        return raw.strip()[:125]

    def generate_reply(self, ctx):
        system, user = prompts.reply_prompt(ctx)
        raw = self._generate(self.model, system, user, [], as_json=False)
        return raw.strip()

    def rewrite_caption(self, ctx):
        system, user = prompts.rewrite_prompt(ctx)
        raw = self._generate(self.model, system, user, [], as_json=False)
        text = strip_fences(raw).strip()
        if not text:
            raise AIPermanent(
                "Gemini returned an empty rewrite - try again or a different "
                "model.")
        return text

    def check_media(self, ctx):
        media = list(ctx.media) + list(ctx.references) + list(ctx.guidelines)
        system, user = prompts.media_check_prompt(ctx)
        raw = self._generate(self.model, system, user, media, as_json=True)
        data = extract_json(raw)
        if data is None:
            # QA is advisory - a garbled/empty reply becomes a visible "review
            # manually" note rather than a hard failure.
            return [Finding(
                severity="info", category="spec",
                message="Automated QA couldn't read the AI response — please "
                        "review this file manually.")]
        out = []
        for f in (data.get("findings") or []):
            if not isinstance(f, dict):
                continue
            out.append(Finding(
                severity=str(f.get("severity") or "info"),
                category=str(f.get("category") or "general"),
                message=str(f.get("message") or ""),
            ))
        return out
