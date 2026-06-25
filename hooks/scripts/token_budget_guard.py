#!/usr/bin/env python3
"""Track transcript-based token cost and enforce budget — PreToolUse hook."""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import load_config, read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(
    os.environ.get("PIPELINE_RUNTIME_CONFIG")
    or (_ROOT / "hooks" / "runtime-config.json")
)

# Model pricing per million tokens (USD)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-20250514": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-opus-4-20250514": {
        "input": 15.0,
        "output": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-haiku-3-20250307": {
        "input": 0.80,
        "output": 4.0,
        "cache_write": 1.0,
        "cache_read": 0.08,
    },
    # Fallback pricing (sonnet-level)
    "default": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
}


def get_pricing(model: Any) -> dict[str, float]:
    if not model:
        return MODEL_PRICING["default"]
    for key in MODEL_PRICING:
        if key == "default":
            continue
        parts = key.split("-")
        prefix = parts[0] + "-" + parts[1]
        if key in model or model.startswith(prefix):
            return MODEL_PRICING[key]
    # Try partial matching
    if "opus" in model:
        return MODEL_PRICING["claude-opus-4-20250514"]
    if "haiku" in model:
        return MODEL_PRICING["claude-haiku-3-20250307"]
    if "sonnet" in model:
        return MODEL_PRICING["claude-sonnet-4-20250514"]
    return MODEL_PRICING["default"]


def safe_number(value: Any) -> float:
    """Coerce an arbitrary value to a finite number.

    Returns 0 for NaN, Infinity, -Infinity, None, objects, arrays, or strings
    that don't parse as finite numerics. Used to make token-count arithmetic
    resistant to malformed transcript entries and malicious string inputs.
    """
    if isinstance(value, bool):
        return 0.0
    if value is None or isinstance(value, (dict, list)):
        return 0.0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) else 0.0


def calculate_cost(usage: dict[str, Any], model: Any) -> float:
    pricing = get_pricing(model)
    # All four fields routed through safe_number — malformed transcript entries
    # or attacker-supplied strings coerce to 0 instead of NaN-poisoning the
    # total cost and silently bypassing the budget check downstream.
    input_tokens = safe_number(usage.get("input_tokens"))
    output_tokens = safe_number(usage.get("output_tokens"))
    cache_write = safe_number(usage.get("cache_creation_input_tokens"))
    cache_read = safe_number(usage.get("cache_read_input_tokens"))

    return (
        (input_tokens * pricing["input"]) / 1_000_000
        + (output_tokens * pricing["output"]) / 1_000_000
        + (cache_write * pricing["cache_write"]) / 1_000_000
        + (cache_read * pricing["cache_read"]) / 1_000_000
    )


def sum_transcript_cost(transcript_path: Any) -> float:
    if not transcript_path:
        return 0.0
    path = Path(transcript_path)
    if not path.exists():
        return 0.0

    total_cost = 0.0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                entry: Any = json.loads(line)
                if isinstance(entry, dict) and entry.get("usage"):
                    total_cost += calculate_cost(entry["usage"], entry.get("model"))
            except Exception:
                # Skip malformed lines
                continue

    return total_cost


def main() -> None:
    # Read hook input from stdin
    raw_input = read_stdin()

    try:
        default: dict[str, Any] = {
            "token_budget_guard": {"max_extra_usage_usd": 5.0, "warn_at_pct": 80}
        }
        config = {**default, **load_config(CONFIG_PATH)}

        guard_config: dict[str, Any] = config.get("token_budget_guard") or {}
        max_budget = guard_config.get("max_extra_usage_usd") or 5.0
        warn_pct = guard_config.get("warn_at_pct") or 80

        hook_input: Any = json.loads(raw_input or "{}")
        transcript_path = hook_input.get("transcript_path")

        total_cost = sum_transcript_cost(transcript_path)

        # Defense-in-depth: if calculate_cost produced NaN or Infinity despite
        # the safe_number guard (e.g. a pricing lookup issue), treat it as
        # budget-exceeded. Fail-closed rather than letting non-finite math
        # silently bypass the comparison.
        if not math.isfinite(total_cost):
            sys.stdout.write(
                json.dumps(
                    {
                        "permissionDecision": "deny",
                        "deny_reason": (
                            "Token budget guard: computed cost was non-finite "
                            "— refusing to proceed. Inspect transcript_path "
                            "and retry."
                        ),
                    }
                )
            )
            sys.exit(0)

        usage_pct = (total_cost / max_budget) * 100

        if total_cost >= max_budget:
            sys.stdout.write(
                json.dumps(
                    {
                        "permissionDecision": "deny",
                        "deny_reason": (
                            f"Token budget exceeded: ${total_cost:.2f} spent "
                            f"of ${max_budget:.2f} limit ({usage_pct:.0f}%). "
                            f"Stop and review before continuing."
                        ),
                    }
                )
            )
        elif usage_pct >= warn_pct:
            sys.stdout.write(
                json.dumps(
                    {
                        "permissionDecision": "allow",
                        "systemMessage": (
                            f"Token budget warning: ${total_cost:.2f} of "
                            f"${max_budget:.2f} used ({usage_pct:.0f}%). "
                            f"Approaching limit."
                        ),
                    }
                )
            )
        else:
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
    except Exception:
        # On error, allow to avoid blocking pipeline
        sys.stdout.write(json.dumps({"permissionDecision": "allow"}))


if __name__ == "__main__":
    main()
