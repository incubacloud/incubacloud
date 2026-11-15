"""The first steps card's calls (``models/cloud_first_step.py``).

Both are about the signed-in user: what the card shows them, and whether
they closed it. A layer inherits the controller to add its own calls.
"""
from odoo import http
from odoo.http import request


class FirstSteps(http.Controller):
    """``/cloud/first_steps`` and closing or opening the card again."""

    @http.route(
        "/cloud/first_steps", type="jsonrpc", auth="user", methods=["POST"],
    )
    def cloud_first_steps(self):
        """Return what the first steps card shows the signed-in user.

        :return: ``cloud.first.steps._state``: ``{enabled: False}`` without
            the Manager role or with the steps off
        :rtype: dict
        """
        return request.env["cloud.first.steps"]._state(request.env.user)

    @http.route(
        "/cloud/first_steps/set_hidden", type="jsonrpc", auth="user",
        methods=["POST"],
    )
    def cloud_first_steps_set_hidden(self, hidden):
        """Close the card for the signed-in user, or open it again.

        :param bool hidden: close it
        :return: the card's state after the change, ``{enabled: False}``
            where the user does not get the card
        :rtype: dict
        """
        Steps = request.env["cloud.first.steps"]
        user = request.env.user
        if not Steps._enabled() or not user.has_group("incubacloud.group_cloud_manager"):
            return {"enabled": False}
        Steps._set_display(user, bool(hidden))
        return Steps._state(user)
