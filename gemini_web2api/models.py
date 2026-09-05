"""Model definitions and mapping from Gemini frontend JS source."""

# MODE_CATEGORY enum from 028-6eb337387583.js:
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

# Captured from Gemini Chat's mode picker on 2026-09-05. The Web protocol uses
# slot 79 for the selected mode and slot 80 for normal (1) versus extended (2)
# reasoning. This is account and rollout dependent, so clients must treat this
# list as a compatibility surface rather than a Google model catalogue.
MODELS = {
    "gemini-3.5-flash-lite": {
        "mode": 6, "think": 0, "extra": {80: 1},
        "desc": "Gemini Chat 3.5 Flash-Lite",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 0, "extra": {80: 1},
        "desc": "Gemini Chat 3.6 Flash",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 0, "extra": {80: 1},
        "desc": "Gemini Chat 3.1 Pro",
    },
}

# Older public names remain accepted but are deliberately omitted from
# /v1/models. They avoid silently routing existing clients to a different mode.
MODEL_ALIASES = {
    "gemini-flash-lite": "gemini-3.5-flash-lite",
    "gemini-3.5-flash": "gemini-3.6-flash",
    "gemini-3.7-flash": "gemini-3.6-flash",
    "gemini-3.1-pro-enhanced": "gemini-3.1-pro",
}


def resolve_model(model_name: str, reasoning=None, default: str = "gemini-3.6-flash"):
    """Resolve model name to (name, mode_id, think_mode, error, extra_fields).

    Unknown model names fall back to default rather than erroring,
    since upstream clients may request arbitrary model identifiers.
    """
    think_override = None
    if "@think=" in model_name:
        model_name, think_str = model_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError:
            return None, None, None, f"Invalid think level: {think_str}", None
    model_name = MODEL_ALIASES.get(model_name, model_name)
    cfg = MODELS.get(model_name)
    if not cfg:
        from .gemini import log
        log(f"Unknown model '{model_name}', falling back to '{default}'")
        model_name = default
        cfg = MODELS[default]
    if reasoning is not None:
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else reasoning
        if effort not in ("none", "low", "medium", "high"):
            return None, None, None, "reasoning effort must be none, low, medium, or high", None
        # Gemini Chat's captured mode picker uses 1 for normal and 2 for
        # Extended thinking. Preserve an explicit @think= override for clients
        # that need the protocol-level value at slot 17.
        reasoning_extra = {80: 1 if effort in ("none", "low") else 2}
    else:
        reasoning_extra = {}

    mode_id = cfg["mode"]
    think_mode = think_override if think_override is not None else cfg["think"]
    extra = dict(cfg.get("extra", {}))
    extra.update(reasoning_extra)
    return model_name, mode_id, think_mode, None, extra or None
