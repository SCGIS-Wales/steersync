"""Interactive overview, push, pull and restore for ``steersync fleet``."""

import click


@click.command("interactive")
@click.pass_context
def interactive(ctx: click.Context) -> None:
    """The interactive overview."""
    from steersync.cli_fleet import fleet_status  # noqa: PLC0415

    ctx.invoke(fleet_status)
