"""OpenAI backend. Uses the official openai SDK's Responses API.

Imported lazily (only when AI is on, not simulating, and the resolved provider
for a task is 'openai'), so the dependency stays inert otherwise and this
module is import-safe even when openai is not installed. Vision + PDFs are sent
as base64 data URLs / input_file parts.
"""
import base64
import json

from app.ai import prompts
from app.ai.base import AIProvider, CaptionResult, Finding
from app.ai.errors import AIAuth, AIPermanent, AITransient


def _sdk():
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - only when dep absent
        raise AIPermanent(
            "openai is not installed; run pip install -r requirements.txt"
        ) from exc
    return openai


def _strip_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


class OpenAIProvider(AIProvider):
    key = "openai"

    def _client(self):
        if not self.api_key:
            raise AIAuth("No OpenAI API key configured.")
        return _sdk().OpenAI(api_key=self.api_key, timeout=self.timeout_s)

    def _content(self, user_text, media):
        """Responses-API content parts: text + image/PDF data URLs."""
        parts = [{"type": "input_text", "text": user_text}]
        for m in media or []:
            b64 = base64.b64encode(m.data).decode("ascii")
            url = f"data:{m.mime_type};base64,{b64}"
            if m.mime_type == "application/pdf":
                parts.append({"type": "input_file", "filename":
                              (m.label or "document") + ".pdf", "file_data": url})
            else:
                parts.append({"type": "input_image", "image_url": url})
        return parts

    def _generate(self, system, user_text, media, *, as_json):
        # JSON is requested in the prompt (the system text demands "ONLY JSON")
        # and parsed defensively by the callers, rather than via a
        # response-format API param whose exact shape drifts across SDK
        # versions - so this adapter stays correct as the SDK moves. `as_json`
        # is kept for symmetry with the Gemini adapter.
        client = self._client()
        openai = _sdk()
        kwargs = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": self._content(user_text, media)}],
            "max_output_tokens": self.max_tokens,
        }
        try:
            resp = client.responses.create(**kwargs)
        except openai.APIStatusError as exc:
            raise self._map_status(exc.status_code)
        except openai.APIConnectionError as exc:
            raise AITransient("OpenAI connection failed") from exc
        except Exception as exc:  # noqa: BLE001
            raise AIPermanent("OpenAI request failed") from exc
        return getattr(resp, "output_text", "") or ""

    @staticmethod
    def _map_status(status):
        msg = f"OpenAI request failed ({status})"
        if status in (401, 403):
            return AIAuth(msg)
        if status in (429, 500, 502, 503, 504):
            return AITransient(msg)
        return AIPermanent(msg)

    # -- capabilities -------------------------------------------------------

    def generate_caption(self, ctx):
        system, user = prompts.caption_prompt(ctx)
        raw = self._generate(system, user, ctx.media, as_json=True)
        try:
            data = json.loads(_strip_json(raw))
        except (ValueError, TypeError) as exc:
            raise AIPermanent("OpenAI returned an unreadable caption.") from exc
        hashtags = [h.lstrip("#") for h in (data.get("hashtags") or [])
                    if isinstance(h, str)]
        per = {k: v for k, v in (data.get("per_platform") or {}).items()
               if isinstance(v, str)}
        return CaptionResult(
            caption=str(data.get("caption") or ""),
            per_platform=per,
            hashtags=hashtags,
            first_comment=str(data.get("first_comment") or ""),
        )

    def generate_alt_text(self, image):
        system, user = prompts.alt_text_prompt(image)
        raw = self._generate(system, user, [image], as_json=False)
        return raw.strip()[:125]

    def check_media(self, ctx):
        media = list(ctx.media) + list(ctx.guidelines)
        system, user = prompts.media_check_prompt(ctx)
        raw = self._generate(system, user, media, as_json=True)
        try:
            data = json.loads(_strip_json(raw))
        except (ValueError, TypeError) as exc:
            raise AIPermanent("OpenAI returned unreadable findings.") from exc
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
