/** @odoo-module **/

import {
    Component,
    onMounted,
    onWillStart,
    onWillUnmount,
    useEffect,
    useRef,
    useState,
} from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";
import {
    LINK_HOSTS,
    cardStateLabel,
    chatLabel,
    linkify,
    mergeEvent,
    messageView,
    modelOptions,
    noticeText,
    noticeTitle,
    providerWording,
    screenContext,
    stateView,
    usageLabel,
} from "../assistant_format";

/** The assistant's side panel: usage, model, conversations, chat, cards. */
export class AssistantPanel extends Component {
    static template = "incubacloud_assistant.AssistantPanel";
    static props = { status: Object, onClose: Function, onChange: Function };

    /** Load the conversations; follow the open one live. */
    setup() {
        this.state = useState({
            view: "list",
            conversations: [],
            conversation: null,
            messages: [],
            progress: "",
            text: "",
            sending: false,
            error: "",
            noticeAccepted: !this.props.status.notice_needed,
            modelId: this.props.status.default_model,
            // The conversation whose deletion waits for a second click.
            deleting: null,
        });
        this.messagesRef = useRef("messages");
        // The latest message in sight as messages come.
        useEffect(
            () => {
                const list = this.messagesRef.el;
                if (list) {
                    list.scrollTop = list.scrollHeight;
                }
            },
            () => [this.state.messages.length, this.state.view]
        );
        this.locale = (user.lang || "en_US").replace("_", "-");
        this.bus = useService("bus_service");
        this._onEvent = (payload) => this.onEvent(payload);
        this._onKey = (ev) => {
            if (ev.key === "Escape") {
                ev.preventDefault();
                this.props.onClose();
            }
        };
        onWillStart(() => this.loadConversations());
        onMounted(() => {
            this.bus.subscribe("cloud_assistant", this._onEvent);
            document.addEventListener("keydown", this._onKey);
        });
        onWillUnmount(() => {
            this.bus.unsubscribe("cloud_assistant", this._onEvent);
            document.removeEventListener("keydown", this._onKey);
        });
    }

    /** Run *call*; show its error in the panel instead of throwing. */
    async _safely(call) {
        this.state.error = "";
        try {
            return await call();
        } catch {
            this.state.error = _t("The panel could not reach the assistant. Try again.");
            return null;
        }
    }

    /** The person's conversations of the last 30 days. */
    async loadConversations() {
        const data = await this._safely(() => rpc("/cloud/assistant/conversations", {}));
        this.state.conversations = data?.conversations || [];
    }

    /** Open a conversation with its messages. */
    async open(id) {
        const data = await this._safely(() =>
            rpc("/cloud/assistant/conversation", { conversation_id: id })
        );
        if (!data) {
            return;
        }
        Object.assign(this.state, {
            conversation: data.conversation,
            messages: data.messages,
            modelId: data.conversation.model_id || this.props.status.default_model,
            progress: "",
            view: "chat",
        });
        this.props.onChange();
    }

    /** Start a new conversation (it is created with its first message). */
    newConversation() {
        Object.assign(this.state, {
            conversation: null,
            messages: [],
            progress: "",
            view: "chat",
            modelId: this.props.status.default_model,
        });
    }

    /** Back to the list of conversations. */
    async backToList() {
        this.state.view = "list";
        await this.loadConversations();
    }

    /** The person read the notice. */
    async acceptNotice() {
        const result = await this._safely(() => rpc("/cloud/assistant/accept_notice", {}));
        if (result?.ok) {
            this.state.noticeAccepted = true;
            this.props.onChange();
        }
    }

    /** Send what the person wrote, with the screen they have open. */
    async send() {
        const text = this.state.text.trim();
        if (!text || this.state.sending) {
            return;
        }
        this.state.sending = true;
        const result = await this._safely(() =>
            rpc("/cloud/assistant/send", {
                text,
                conversation_id: this.state.conversation?.id || null,
                context: screenContext(this.env.currentRoute),
                model_id: this.state.modelId,
            })
        );
        this.state.sending = false;
        if (!result) {
            return;
        }
        if (!result.ok) {
            this.state.error = result.error;
            return;
        }
        this.state.text = "";
        this.state.conversation = result.conversation;
        this._merge({ conversation_id: result.conversation.id, kind: "message", message: result.message });
    }

    /** Send on Enter, a new line on Shift+Enter. */
    onInputKey(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
        }
    }

    /** Go on after the step cap (once: the button goes with the next message). */
    continueTask() {
        if (this.state.sending) {
            return;
        }
        this.state.text = _t("Go on.");
        return this.send();
    }

    /** Stop the open conversation's task. */
    async stop() {
        await this._call("/cloud/assistant/stop");
    }

    /** Go on after an error or a stop. */
    async retry() {
        await this._call("/cloud/assistant/retry");
    }

    /** Delete a conversation. */
    async remove(id) {
        if (this.state.deleting !== id) {
            // A first click asks; the second deletes.
            this.state.deleting = id;
            return;
        }
        this.state.deleting = null;
        await this._safely(() => rpc("/cloud/assistant/delete", { conversation_id: id }));
        if (this.state.conversation?.id === id) {
            this.state.conversation = null;
            this.state.view = "list";
        }
        await this.loadConversations();
    }

    /** Change the model of the open conversation (or of the next one). */
    async setModel(id) {
        this.state.modelId = id;
        if (this.state.conversation) {
            const result = await this._safely(() =>
                rpc("/cloud/assistant/set_model", { conversation_id: this.state.conversation.id, model_id: id })
            );
            if (result && !result.ok) {
                this.state.error = result.error;
            }
        }
    }

    /** Call a route of the open conversation and keep its new state. */
    async _call(route) {
        const result = await this._safely(() =>
            rpc(route, { conversation_id: this.state.conversation.id })
        );
        if (result?.conversation) {
            this.state.conversation = result.conversation;
        }
    }

    /** The person answers a card. */
    async answerCard(id, confirm) {
        const route = confirm ? "/cloud/assistant/confirm" : "/cloud/assistant/cancel";
        const result = await this._safely(() => rpc(route, { message_id: id }));
        if (result && !result.ok) {
            this.state.error = result.error;
        }
        await this.open(this.state.conversation.id);
    }

    /** A bus event: the open conversation's, or a state change of another one. */
    onEvent(payload) {
        const open = this.state.conversation;
        if (open && payload?.conversation_id === open.id) {
            this._merge(payload);
            if (payload.kind === "state" && payload.unread && this.state.view === "chat") {
                // Seen as it happens: read it, so the button's dot goes.
                this.open(open.id);
            }
        } else if (payload?.kind === "state" && this.state.view === "list") {
            this.loadConversations();
        }
    }

    /** Merge an event into the open conversation. */
    _merge(event) {
        const merged = mergeEvent(
            {
                conversationId: this.state.conversation?.id,
                messages: this.state.messages,
                progress: this.state.progress,
                state: this.state.conversation?.state,
            },
            event
        );
        this.state.messages = merged.messages;
        this.state.progress = merged.progress;
        if (this.state.conversation && merged.state) {
            this.state.conversation.state = merged.state;
        }
    }

    /** The month's usage line. */
    get usage() {
        return usageLabel(this.props.status.usage, this.locale);
    }

    /** The provider as the notices name it. */
    get wording() {
        return providerWording(this.props.status);
    }

    /** The first-use notice's title. */
    get noticeTitle() {
        return noticeTitle();
    }

    /** The first-use notice. */
    get notice() {
        return noticeText(this.wording);
    }

    /** The fixed label next to the message field. */
    get aiLabel() {
        return chatLabel(this.wording);
    }

    /** The allowed models for the selector. */
    get models() {
        return modelOptions(this.props.status.models);
    }

    /** The open conversation's state and its buttons. */
    get stateInfo() {
        return stateView(this.state.conversation?.state);
    }

    /** The messages the chat shows, with how it shows them. */
    get views() {
        return this.state.messages
            .map((message) => ({ message, view: messageView(message) }))
            .filter((item) => item.view);
    }

    /** The plain parts and the links of the model's text: known hosts only. */
    parts(text) {
        return linkify(text, this.props.status.link_hosts || LINK_HOSTS);
    }

    /** Whether the panel only reads: the assistant is off for the person. */
    get readOnly() {
        return !this.props.status.available;
    }

    /** A conversation's name in the list. */
    conversationName(conversation) {
        return conversation.name || _t("Conversation");
    }

    /** How a closed card closed. */
    cardState(state) {
        return cardStateLabel(state);
    }
}
