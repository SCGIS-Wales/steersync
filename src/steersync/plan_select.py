"""The single checkbox that approves a plan (questionary, loaded on demand)."""

from collections.abc import Callable
from typing import Any

from steersync.plan import Plan

Checkbox = Callable[..., Any]


def _questionary_checkbox(message: str, choices: list[Any]) -> list[str] | None:
    import questionary  # noqa: PLC0415 - interface library, loaded on demand

    result: list[str] | None = questionary.checkbox(message, choices=choices).ask()
    return result


def select_steps(
    plan: Plan,
    remembered: frozenset[str] | None,
    *,
    checkbox: Checkbox = _questionary_checkbox,
    force: bool = False,
) -> list[str] | None:
    """One checkbox for the whole plan; None when the user cancels.

    Enter is the approval. Remembered steps (``setup.last``) are preselected instead of the
    defaults. Destructive steps are disabled unless ``force``.
    """
    import questionary  # noqa: PLC0415 - interface library, loaded on demand

    choices = [
        questionary.Choice(
            title=f"{step.title}  ({step.detail})" if step.detail else step.title,
            value=step.id,
            # questionary preselects with checked=True; its `default` only moves the cursor.
            checked=(step.id in remembered) if remembered is not None else step.default,
            disabled="needs --force" if step.risk == "destructive" and not force else None,
        )
        for step in plan.steps
        if not step.done
    ]
    picked: list[str] | None = checkbox(
        "Select the changes to apply (space toggles, enter applies):", choices=choices
    )
    return picked
