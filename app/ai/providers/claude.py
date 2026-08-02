"""Anthropic Claude backend. Uses the official `anthropic` SDK's Messages API.

Imported lazily (only when AI is on, not simulating, and the resolved provider
for a task is 'claude'), so the dependency stays inert otherwise and this
module is import-safe even when `anthropic` is not installed. Vision + PDFs are
sent as base64 image/document content blocks.
"""
import base64
import json

from app.ai import prompts
from app.ai.base import AIProvider, CaptionResult, Finding
from app.ai.errors import AIAuth, AIPermanent, AITransient


def _sdk():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - only when dep absent
        raise AIPermanent(
            "anthropic is not installed; run pip install -r requirements.txt"
        ) from exc
    return anthropic


def _strip_json(text):
    """Model JSON, tolerant of ```json fences."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


class ClaudeProvider(AIProvider):
    key = "claude"

    def _client(self):
        if not self.api_key:
            raise AIAuth("No Anthropic API key configured.")
        return _sdk().Anthropic(api_key=self.api_key, timeout=self.timeout_s)

    def _content(self, user_text, media):
        """Messages-API content blocks: text + image/document (base64)."""
        blocks = [{"type": "text", "text": user_text}]
        for m in media or []:
            b64 = base64.b64encode(m.data).decode("ascii")
            source = {"type": "base64", "media_type": m.mime_type, "data": b64}
            if m.mime_type == "application/pdf":
                blocks.append({"type": "document", "source": source})
            else:
                blocks.append({"type": "image", "source": source})
        return blocks

    def _generate(self, system, user_text, media, *, as_json):
        # JSON is requested in the prompt (the system text demands "ONLY JSON")
        # and parsed defensively by the callers, rather than via a
        # response-format API param - so this adapter stays correct as the SDK
        # moves. `as_json` is kept for symmetry with the other adapters.
        client = self._client()          # raises AIAuth on a missing key first
        anthropic = _sdk()
        try:
            msg = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user",
                           "content": self._content(user_text, media)}],
            )
        except anthropic.APIStatusError as exc:
            raise self._map_status(getattr(exc, "status_code", None))
        except anthropic.APIConnectionError as exc:
            raise AITransient("Anthropic connection failed") from exc
        except Exception as exc:  # noqa: BLE001 - never surface a key
            raise AIPermanent("Anthropic request failed") from exc

        usage = getattr(msg, "usage", None)
        if usage is not None:
            self._add_usage(getattr(usage, "input_tokens", 0),
                           getattr(usage, "output_tokens", 0))
        # Concatenate the text blocks of the response (ignore any non-text).
        parts = [getattr(b, "text", "") for b in (getattr(msg, "content", None) or [])
                 if getattr(b, "type", None) == "text"]
        return "".join(parts)

    @staticmethod
    def _map_status(status):
        msg = f"Anthropic request failed ({status})" if status \
            else "Anthropic request failed"
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
            raise AIPermanent("Claude returned an unreadable caption.") from exc
        hashtags = [h.lstrip("#") for h in (data.get("hashtags") or [])
                    if isinstance(h, str)]
        per = {k: v for k, v in (data.get("per_platform") or {}).items()
               if isinstance(v, str)}
        variations = [v for v in (data.get("variations") or [])
                      if isinstance(v, str) and v.strip()]
        return CaptionResult(
            caption=str(data.get("caption") or ""),
            per_platform=per,
            hashtags=hashtags,
            first_comment=str(data.get("first_comment") or ""),
            variations=variations,
        )

    def generate_alt_text(self, image):
        system, user = prompts.alt_text_prompt(image)
        raw = self._generate(system, user, [image], as_json=False)
        return raw.strip()[:125]

    def generate_reply(self, ctx):
        system, user = prompts.reply_prompt(ctx)
        raw = self._generate(system, user, [], as_json=False)
        return raw.strip()

    def check_media(self, ctx):
        media = list(ctx.media) + list(ctx.guidelines)
        system, user = prompts.media_check_prompt(ctx)
        raw = self._generate(system, user, media, as_json=True)
        try:
            data = json.loads(_strip_json(raw))
        except (ValueError, TypeError) as exc:
            raise AIPermanent("Claude returned unreadable findings.") from exc
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
