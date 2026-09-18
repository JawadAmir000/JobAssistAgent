"""Per-million-token USD prices for API-billed models.

    cost(model, in_tok, out_tok) -> (usd, estimated)

Unknown models fall back to Haiku pricing with estimated=True.
"""
from __future__ import annotations

# (input USD per 1M tokens, output USD per 1M tokens)
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-1": (15.00, 75.00),
}
FALLBACK_MODEL = "claude-haiku-4-5"


def _lookup(model: str) -> tuple[float, float] | None:
    if model in PRICES:
        return PRICES[model]
    # tolerate dated suffixes like "claude-haiku-4-5-20251001" and family aliases
    for key, price in PRICES.items():
        if model.startswith(key):
            return price
    low = model.lower()
    for family in ("haiku", "sonnet", "opus"):
        if family in low:
            for key, price in PRICES.items():
                if family in key:
                    return price
    return None


def cost(model: str, in_tok: int, out_tok: int) -> tuple[float, bool]:
    """Return (usd, estimated). estimated=True when the model's price is unknown."""
    price = _lookup(model or "")
    estimated = price is None
    if price is None:
        price = PRICES[FALLBACK_MODEL]
    in_price, out_price = price
    usd = (max(in_tok, 0) * in_price + max(out_tok, 0) * out_price) / 1_000_000
    return round(usd, 8), estimated
