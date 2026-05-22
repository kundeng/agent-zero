"""GET /channels_wizard — per-provisioner wizard step list (spec 08 1.8).

Returns the JSON serialization of the requested provisioner's
``wizard_steps()``. The UI calls this once when the user clicks
"Provision" on a channel card; the CLI shim calls it on
``haz channel provision <platform> --help`` to derive the flag set.

The query parameter ``channel_type`` selects the provisioner.
"""

from typing import Any

from python.helpers.api import ApiHandler, Request, Response


class ChannelsWizard(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(
        self, input: dict[str, Any], request: Request
    ) -> dict[str, Any] | Response:
        from hyperagent0.channels.provision.dispatch import (
            ensure_provisioners_loaded,
            wizard_steps_for,
        )

        ensure_provisioners_loaded()

        channel_type = (
            input.get("channel_type")
            or request.args.get("channel_type", "")
        ).strip()
        if not channel_type:
            return {"success": False, "error": "channel_type is required"}

        try:
            steps = wizard_steps_for(channel_type)
        except LookupError as exc:
            return {"success": False, "error": str(exc)}

        return {"success": True, "channel_type": channel_type, "steps": steps}
