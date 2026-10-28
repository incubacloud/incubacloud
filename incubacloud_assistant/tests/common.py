"""Shared fixtures of the assistant's tests: users, settings, conversations."""

import json
from unittest.mock import MagicMock, patch

from odoo import fields

from odoo.addons.incubacloud_mcp.tests.common import McpUsersMixin, PanelFixtureMixin

from ..connectors.anthropic_connector import AnthropicConnector
from ..connectors.base import ChatResponse, ToolCall, Usage
from ..models import assistant_engine
from ..models.cloud_settings import BLOCKED_PARAM

#: The provider key of the tests: a secret, as a real one.
KEY = "sk-ant-assistant-test-key-0001"
#: The models the provider lists in the tests.
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")


def listing(ids=MODELS, tools=True, price_in=None, price_out=None):
    """Return a provider's listing of *ids*, as the settings keep it."""
    return [
        {
            "id": model,
            "name": model,
            "tools": tools,
            "price_in": price_in,
            "price_out": price_out,
        }
        for model in ids
    ]


class AssistantMixin(McpUsersMixin, PanelFixtureMixin):
    """The panel's assistant switched on for some users."""

    def _configure(
        self,
        users=(),
        provider="anthropic",
        models=MODELS,
        allowed=MODELS[:2],
        default=MODELS[0],
        limit=0.0,
        step_cap=30,
        enabled=True,
        key=KEY,
        terms=True,
    ):
        """Switch the assistant on for *users*; return the settings.

        The parameter that blocks it (IncubaCloud's own panel) goes: these
        tests try the module itself, in every mode its CI installs it.

        *terms*: section 12A accepted, as switching it on in Settings leaves it.
        """
        self.env["ir.config_parameter"].sudo().set_param(BLOCKED_PARAM, False)
        settings = self.env["cloud.settings"]._assistant_settings()
        settings.write(
            {
                "assistant_enabled": enabled,
                "assistant_provider": provider,
                "assistant_api_key": key,
                "assistant_models": json.dumps(listing(models)),
                "assistant_allowed_models": json.dumps(list(allowed)),
                "assistant_default_model": default,
                "assistant_user_ids": [(6, 0, [user.id for user in users])],
                "assistant_monthly_limit": limit,
                "assistant_step_cap": step_cap,
                "assistant_terms_accepted_on": fields.Datetime.now()
                if terms
                else False,
                "assistant_terms_version": False,
                "assistant_terms_accepted_by": "Test (asis-test)" if terms else False,
                "assistant_terms_accepted_ip": "127.0.0.1" if terms else False,
            }
        )
        # The secret filter reads the ciphertexts with SQL: they must be there.
        self.env.flush_all()
        return settings


def answer(text="", calls=(), stop=None, usage=None, model="claude-opus-5-5"):
    """A provider's answer: *text*, and *calls* as ``(id, name, arguments)``."""
    tool_calls = tuple(ToolCall(*call) for call in calls)
    return ChatResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage or Usage(1000, 50),
        model=model,
        stop=stop or ("tool_use" if tool_calls else "end"),
    )


class EngineMixin(AssistantMixin):
    """Run the engine's steps against a connector that answers as told.

    The connector is a ``MagicMock(spec=AnthropicConnector)``; the engine's
    pauses are patched out. Steps run in registry test mode: their own
    cursors are the test's.
    """

    def _provider(self, *answers):
        """Make the provider answer *answers* in turn; return the connector mock.

        Patched once per test: patching again with autospec fails. A later
        call gives the same connector new answers.
        """
        if not hasattr(self, "connector"):
            self.connector = MagicMock(spec=AnthropicConnector)
            patcher = patch.object(
                assistant_engine,
                "connector_for",
                autospec=True,
                return_value=self.connector,
            )
            self.connector_for = patcher.start()
            self.addCleanup(patcher.stop)
            sleeper = patch.object(assistant_engine, "_sleep", autospec=True)
            self.sleep = sleeper.start()
            self.addCleanup(sleeper.stop)
        self.connector.reset_mock()
        self.connector.chat.side_effect = list(answers)
        return self.connector

    def _step(self, conversation, job_uuid=None):
        """Run one step of *conversation*, as its owner (as job *job_uuid*)."""
        owner = conversation.sudo().user_id
        engine = self.env["incubacloud.assistant.engine"].with_user(owner)
        if job_uuid:
            engine = engine.with_context(job_uuid=job_uuid)
        # A tool binds a request of its own (the runner's, the user's): the
        # test framework would refuse the cursors of a request without its cookie.
        with (
            patch.object(self, "http_request_allow_all", True),
            self.enter_registry_test_mode(),
        ):
            engine._step(conversation.with_user(owner))
        conversation.invalidate_recordset()

    def _working(self, user, text="Hello", context=None):
        """A conversation of *user* whose task was just started by *text*."""
        conversation = (
            self.env["incubacloud.assistant.conversation"].with_user(user)._start()
        )
        conversation._post(text, context or {})
        return conversation

    def _sent(self, connector, call=-1):
        """Return the ``ChatRequest`` of the provider's call *call*."""
        return connector.chat.call_args_list[call].args[0]
