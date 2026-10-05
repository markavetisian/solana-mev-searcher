"""Version identifiers stamped onto every feature row, score, decision, order and trade.

Bump the matching constant whenever logic changes in a way that changes outputs for identical inputs.
`configuration_version` (hash of the effective config) and `ai_prompt_version` (hash of the system prompt)
are computed at runtime.
"""

STRATEGY_VERSION = "pump-momentum-0.1.0"
FEATURE_VERSION = "features-1.0.0"
SCORING_VERSION = "score-1.0.0"
EV_MODEL_VERSION = "ev-empirical-1.0.0"
FILTER_VERSION = "filters-1.0.0"
PUMP_IDL_COMMIT = "cb188ce08b5069196eef1f3e4a0c43b70099793b"  # pump-fun/pump-public-docs, 2026-09-29
