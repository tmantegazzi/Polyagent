import json
import logging
from typing import Optional

import anthropic

from core.models import OrderBook, Position, FairValueEstimate

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert prediction market analyst specializing in fair value estimation for binary outcome markets on Polymarket.

Your role is to analyze the current order book, market context, and position data to estimate the true probability (fair value) of the YES outcome, and recommend appropriate bid-ask spread parameters for market making.

You must respond with a valid JSON object containing exactly these fields:
{
  "fair_value": <float between 0.01 and 0.99>,
  "confidence": <"high" | "medium" | "low">,
  "spread_factor": <float, 1.0 = normal, higher = wider spread>,
  "reasoning": <string, brief explanation>
}

Guidelines:
- fair_value is the probability of YES (1.0 USDC per share at resolution)
- If the order book mid price is reasonable, anchor to it but adjust for your analysis
- confidence should reflect how certain you are about the fair value
- spread_factor: 1.0 for high confidence, 1.5-2.0 for medium, 2.0-3.0 for low confidence or thin markets
- Consider: order book depth, bid-ask spread, market liquidity, event timing
- Be conservative: when uncertain, widen the spread (higher spread_factor)
"""


class ValuationEngine:
    def __init__(self, api_key: str):
        self._client = anthropic.Anthropic(api_key=api_key)

    async def estimate_fair_value(
        self,
        token_id: str,
        order_book: OrderBook,
        position: Position,
        market_info: Optional[dict] = None,
    ) -> FairValueEstimate:
        prompt = self._build_prompt(token_id, order_book, position, market_info)

        try:
            response = self._client.messages.create(
                model="claude-opus-4-7",
                max_tokens=1024,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )

            result = self._parse_response(response)
            return FairValueEstimate(
                fair_value=result["fair_value"],
                confidence=result["confidence"],
                spread_factor=result["spread_factor"],
                reasoning=result["reasoning"],
            )

        except Exception as e:
            logger.error("AI valuation failed: %s", e)
            # Fallback: use mid price from order book
            mid = order_book.mid_price()
            return FairValueEstimate(
                fair_value=mid if mid is not None else 0.5,
                confidence="low",
                spread_factor=3.0,
                reasoning=f"AI unavailable ({e}), using order book mid price",
            )

    def _build_prompt(
        self,
        token_id: str,
        order_book: OrderBook,
        position: Position,
        market_info: Optional[dict],
    ) -> str:
        bid = order_book.best_bid()
        ask = order_book.best_ask()
        mid = order_book.mid_price()
        spread = order_book.spread()
        liq = order_book.liquidity_at_levels(10)

        top_bids = [{"price": l.price, "size": l.size} for l in order_book.bids[:5]]
        top_asks = [{"price": l.price, "size": l.size} for l in order_book.asks[:5]]

        context = {
            "token_id": token_id,
            "order_book": {
                "best_bid": bid,
                "best_ask": ask,
                "mid_price": mid,
                "spread": spread,
                "top_bids": top_bids,
                "top_asks": top_asks,
                "liquidity_usdc": liq,
            },
            "current_position": {
                "yes_shares": position.yes_shares,
                "no_shares": position.no_shares,
                "realized_pnl": position.realized_pnl,
            },
        }

        if market_info:
            context["market_info"] = {
                k: v for k, v in market_info.items()
                if k in ("question", "description", "end_date_iso", "active", "closed")
            }

        return (
            f"Analyze this prediction market and provide your fair value estimate:\n\n"
            f"```json\n{json.dumps(context, indent=2)}\n```\n\n"
            f"Respond with the JSON object only."
        )

    def _parse_response(self, response) -> dict:
        for block in response.content:
            if block.type == "text":
                text = block.text.strip()
                # Strip markdown code fences if present
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.strip()
                data = json.loads(text)
                fv = float(data["fair_value"])
                assert 0.01 <= fv <= 0.99, "fair_value out of range"
                assert data["confidence"] in ("high", "medium", "low")
                sf = float(data["spread_factor"])
                assert sf >= 1.0
                return {
                    "fair_value": fv,
                    "confidence": data["confidence"],
                    "spread_factor": sf,
                    "reasoning": str(data.get("reasoning", "")),
                }
        raise ValueError("No text block found in AI response")
