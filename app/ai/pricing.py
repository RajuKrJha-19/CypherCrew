"""Approximate per-model pricing, for AI cost estimates only.

These are representative USD prices per 1,000,000 tokens (input, output) and
WILL drift - they exist to give the team a rough, directional spend figure, not
a billing-grade number. An unknown model estimates $0 (tokens are still logged),
so a new model never breaks logging - just add a row here to price it.
"""

# (model-id substring, input $/1M, output $/1M). First match wins; check the
# more specific ids (e.g. "flash-lite") before the general ones.
_PRICES = [
    ("gemini-2.5-flash-lite", 0.05, 0.20),
    ("gemini-2.5-flash", 0.10, 0.40),
    ("gemini-2.5-pro", 1.25, 5.00),
    ("gemini", 0.10, 0.40),            # unknown gemini -> flash-ish
    ("gpt-5-mini", 0.25, 2.00),
    ("gpt-4.1-mini", 0.15, 0.60),
    ("gpt-5", 1.25, 10.00),
    ("gpt", 0.25, 2.00),               # unknown gpt -> mini-ish
    ("claude-haiku", 1.00, 5.00),
    ("claude-sonnet", 3.00, 15.00),
    ("claude-opus", 5.00, 25.00),
]


def rates(model):
    """(input_per_1m, output_per_1m) for a model id, or (0, 0) if unknown."""
    m = (model or "").lower()
    for key, in_rate, out_rate in _PRICES:
        if key in m:
            return in_rate, out_rate
    return 0.0, 0.0


def estimate(model, input_tokens, output_tokens):
    """Rough USD cost for a call. 0 for unknown models / simulation."""
    in_rate, out_rate = rates(model)
    return round((input_tokens or 0) / 1_000_000 * in_rate
                 + (output_tokens or 0) / 1_000_000 * out_rate, 6)
