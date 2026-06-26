"""Provider-qualified model ids: ``<provider>/<model>``.

A model id is either an *agnostic keyword* (``auto`` / ``max`` / ``default`` /
``""``) which pins no provider, or a *qualified* id of the form
``provider/model`` where ``provider`` is a backend name. The model half may
itself contain ``/`` and ``:`` (e.g. ``ollama/hf.co/user/repo``,
``ollama-cloud/gpt-oss:120b``), so we always split on the **first** ``/`` only.

This module is intentionally dependency-free (no import of backends/config) so
it can be used from the scheduler, worker, and manager without cycles.
"""

from __future__ import annotations


AGNOSTIC = frozenset({"auto", "max", "default", ""})


def split_model(model_id: str | None) -> tuple[str | None, str]:
    """Return ``(provider, model)``. ``provider`` is ``None`` for agnostic ids."""
    text = (model_id or "").strip()
    if text.lower() in AGNOSTIC:
        return None, text
    provider, sep, model = text.partition("/")
    if not sep:
        return None, text  # bare/unqualified id (legacy) — no provider
    return provider.strip(), model.strip()


def is_qualified(model_id: str | None) -> bool:
    """True when ``model_id`` carries an explicit ``provider/`` prefix."""
    return split_model(model_id)[0] is not None


def provider_of(model_id: str | None) -> str | None:
    """The provider half, or ``None`` for agnostic/bare ids."""
    return split_model(model_id)[0]


def join_model(provider: str, model: str) -> str:
    """Build a qualified id; raises on an empty provider or model."""
    if not provider or not model:
        raise ValueError("provider and model must both be non-empty")
    return f"{provider}/{model}"
