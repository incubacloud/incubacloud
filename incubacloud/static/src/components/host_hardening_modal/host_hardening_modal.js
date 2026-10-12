/** HostHardeningModal — confirmation for the "Apply Hardening" action.
 *
 *  Loads ``/cloud/preflight_host_hardening`` on open to compute the
 *  warnings and the production gate. Two action buttons when Full Setup
 *  hasn't run on a root host yet ("Hardening only" vs "Hardening + Full
 *  Setup"); otherwise a single one. When ``prod_running > 0`` the
 *  primary button stays disabled until the user types the host name,
 *  and nothing can be queued before the host's SSH key is trusted.
 *
 *  ``offer`` marks the dialog opened on the operator's behalf, right
 *  after a newly connected host became reachable: it says the hardening
 *  is optional and turns Cancel into "Not now".
 */
import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

export class HostHardeningModal extends Component {
    static template = "incubacloud.HostHardeningModal";
    static props = {
        hostId: { type: Number },
        hostName: { type: String },
        offer: { type: Boolean, optional: true },
        onClose: { type: Function },
        onApplied: { type: Function, optional: true },
    };

    setup() {
        this.state = useState({
            loading: true,
            error: null,
            preflight: null,
            prodConfirmInput: "",
            submitting: false,
        });
        onWillStart(() => this._loadPreflight());
    }

    /** Fetch the host's preflight figures into ``state.preflight``. */
    async _loadPreflight() {
        this.state.loading = true;
        try {
            const res = await rpc("/cloud/preflight_host_hardening", {
                host_id: this.props.hostId,
            });
            if (res?.ok) {
                this.state.preflight = res;
            } else {
                this.state.error = res?.error || _t("Preflight failed.");
            }
        } catch {
            this.state.error = _t("Preflight request failed.");
        } finally {
            this.state.loading = false;
        }
    }

    // ── Derived state ──────────────────────────────────────────────

    /** Whether the host runs production, which asks for a typed name. */
    get prodGated() {
        const pf = this.state.preflight;
        return !!pf && pf.prod_running > 0;
    }

    /** Whether the production gate, if any, has been passed. */
    get prodConfirmOk() {
        if (!this.prodGated) {
            return true;
        }
        return this.state.prodConfirmInput.trim() === this.props.hostName;
    }

    /** Whether to offer hardening and Full Setup as one chain. */
    get showSmartSuggestion() {
        const pf = this.state.preflight;
        return (
            !!pf
            && !pf.full_setup_done
            && (pf.connecting_user || "") === "root"
        );
    }

    /** Whether this is the host's first hardening (Docker restarts). */
    get firstRunWarning() {
        const pf = this.state.preflight;
        // live-restore is not active before the first run, so restarting
        // Docker stops the running containers; "hardened before" is the
        // closest thing to knowing the daemon.json the panel has.
        return !!pf && !pf.has_hardened_before;
    }

    /** Whether the host's SSH key is still untrusted. */
    get keyUntrusted() {
        const pf = this.state.preflight;
        return !!pf && !pf.has_known_hosts_key;
    }

    /** Whether the primary action may be clicked now. */
    get canSubmit() {
        return (
            !!this.state.preflight
            && !this.state.preflight.has_active_job
            && !this.keyUntrusted
            && !this.state.submitting
            && this.prodConfirmOk
        );
    }

    // ── Actions ────────────────────────────────────────────────────

    /** Close the dialog unless a request is in flight. */
    cancel() {
        if (this.state.submitting) return;
        this.props.onClose();
    }

    /** Close when the backdrop itself is clicked. */
    onOverlayClick(ev) {
        if (ev.target.classList.contains("ic-modal-overlay")) {
            this.cancel();
        }
    }

    /**
     * Queue the hardening, alone or followed by Full Setup.
     *
     * @param {boolean} chainFullSetup queue Full Setup after it
     */
    async _apply(chainFullSetup) {
        if (!this.canSubmit) return;
        this.state.submitting = true;
        try {
            const res = await rpc("/cloud/apply_host_hardening", {
                host_id: this.props.hostId,
                chain_full_setup: chainFullSetup,
            });
            if (res?.ok) {
                const msg = chainFullSetup
                    ? _t(
                        "Hardening + Full Setup chain queued. "
                        + "Watch the jobs panel for live progress."
                    )
                    : _t(
                        "Hardening queued. Watch the jobs panel for "
                        + "live progress."
                    );
                this.env.toast?.success(msg);
                this.props.onApplied?.(res.job_ids || []);
                this.props.onClose();
            } else {
                this.state.error = res?.error || _t("Failed to queue.");
            }
        } catch {
            this.state.error = _t("Failed to queue hardening.");
        } finally {
            this.state.submitting = false;
        }
    }

    /** Queue the hardening on its own. */
    applyHardeningOnly() {
        return this._apply(false);
    }

    /** Queue the hardening followed by Full Setup. */
    applyHardeningAndFullSetup() {
        return this._apply(true);
    }
}
