import {Component, onWillUnmount, useState} from "@odoo/owl";
import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";
import {agoText} from "../../utils/dates";

/** How often the "read N s ago" line moves on. */
const TICK_MS = 15000;

/**
 * A reading taken from the host when somebody asks for it, never on a
 * timer: a button, the route it calls over SSH, and whatever the slot
 * makes of the answer, with how long ago it was read and a way to read
 * it again.
 *
 * Only one reading of a board runs at a time (``busy``/``onBusy``): each
 * holds a worker of the panel for up to 20 s, and two buttons pressed
 * in a row should not hold two.
 */
export class OnDemandPanel extends Component {
  static template = "incubacloud.OnDemandPanel";
  static props = {
    title: {type: String},
    hint: {type: String, optional: true},
    buttonLabel: {type: String},
    route: {type: String},
    params: {type: Object},
    busy: {type: Boolean, optional: true},
    onBusy: {type: Function, optional: true},
    slots: {type: Object},
  };
  static defaultProps = {hint: "", busy: false};

  setup() {
    this.state = useState({loading: false, error: "", data: null, readAt: 0, now: Date.now()});
    this.timer = setInterval(() => (this.state.now = Date.now()), TICK_MS);
    onWillUnmount(() => clearInterval(this.timer));
  }

  /** Whether the button is to be refused: this or another reading runs. */
  get disabled() {
    return this.state.loading || this.props.busy;
  }

  /** "read 30 s ago", in the reader's language. */
  get readAgo() {
    if (!this.state.readAt) {
      return "";
    }
    return _t("read %s", agoText(Math.max(0, (this.state.now - this.state.readAt) / 1000)));
  }

  /** Ask the host, through the route, and keep the answer. */
  async read() {
    if (this.disabled) {
      return;
    }
    this.state.loading = true;
    this.state.error = "";
    this.props.onBusy?.(true);
    try {
      const res = await rpc(this.props.route, this.props.params);
      if (res.ok) {
        this.state.data = res;
        this.state.readAt = Date.now();
        this.state.now = this.state.readAt;
      } else {
        this.state.error = res.error || _t("Could not read it from the host.");
      }
    } catch (e) {
      this.state.error = e?.data?.message || e?.message || _t("Could not read it from the host.");
    } finally {
      this.state.loading = false;
      this.props.onBusy?.(false);
    }
  }
}
