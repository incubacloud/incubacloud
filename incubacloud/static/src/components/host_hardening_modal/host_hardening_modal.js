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
 *
 *  It always says who will be able to open SSH afterwards, as the
 *  preflight's ``ssh_access`` works it out, and offers the asker's own
 *  address with an "Also allow my IP" box, unticked on every opening:
 *  nothing joins the host's SSH list unless it is ticked.
 */
import { Component, markup, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { announceJob } from "../../utils/job_queue";

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
            // "Also allow my IP", unticked whenever the dialog opens.
            allowMyIp: false,
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

    /**
     * The production gate's request, as one sentence to translate.
     *
     * Two terms around the name read "Tipo <name> para confirmar:" in
     * Spanish: "Type" is also the noun of every type column. The name
     * is escaped and drawn as code inside the translated sentence.
     *
     * @returns {import("@odoo/owl").Markup}
     */
    get prodConfirmPrompt() {
        return _t("Type %(host)s to confirm:", {
            host: markup`<code class="rl-mono">${this.props.hostName}</code>`,
        });
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

    /** What the preflight says about who can open SSH afterwards. */
    get sshAccess() {
        return this.state.preflight?.ssh_access || null;
    }

    /** The banner's severity: a warning where SSH is open or panel-only. */
    get sshAccessLevel() {
        const state = this.sshAccess?.state;
        return state === "open" || state === "panel_only" ? "warning" : "info";
    }

    /**
     * Who will be able to open SSH, as one sentence to translate.
     *
     * The addresses are placeholders of the sentence, never terms of
     * their own around it, so a translation can put them where its
     * grammar wants them.
     *
     * @returns {string}
     */
    get sshAccessText() {
        const access = this.sshAccess;
        if (!access) {
            return "";
        }
        const panel = access.panel_ip;
        switch (access.state) {
            case "open":
                return _t(
                    "SSH on this server will stay open to any address. Only key logins are accepted, and fail2ban bans for an hour any address that fails three times within ten minutes."
                );
            case "edge":
                return _t(
                    "Only IncubaCloud and this panel's server can open SSH on this server: a server IncubaCloud provides admits no other address."
                );
            case "panel_only":
                if (access.person === "allowed") {
                    return _t(
                        "Only this panel's server (%(panel)s) will be able to open SSH on this server. Any other address will be refused.",
                        { panel }
                    );
                }
                return _t(
                    "Only this panel's server (%(panel)s) will be able to open SSH on this server. Any other address, yours included, will be refused.",
                    { panel }
                );
            default: {
                const list = (access.entries || []).join(", ");
                if (!panel) {
                    return _t(
                        "Only this panel's server and these addresses will be able to open SSH on this server: %(list)s.",
                        { list }
                    );
                }
                return _t(
                    "Only this panel's server (%(panel)s) and these addresses will be able to open SSH on this server: %(list)s.",
                    { panel, list }
                );
            }
        }
    }

    /** Whether to offer the asker's own address: the server says it can. */
    get showAllowMyIp() {
        return this.sshAccess?.person === "offer";
    }

    /** The "Also allow my IP" box's label, with the address it adds. */
    get allowMyIpLabel() {
        return _t("Also allow my IP (%(ip)s)", { ip: this.sshAccess?.person_ip || "" });
    }

    /**
     * Why the asker's address is not offered, when it is not.
     *
     * @returns {string} ``""`` when there is nothing to say
     */
    get sshPersonNote() {
        const access = this.sshAccess;
        switch (access?.person) {
            case "allowed":
                return _t("Your IP (%(ip)s) is already allowed.", { ip: access.person_ip });
            case "unknown":
                return _t(
                    "This panel cannot tell your public address, so it cannot offer to allow it."
                );
            case "family":
                return _t(
                    "This panel sees you over %(fam)s (%(ip)s), but SSH to this server goes over %(fam_host)s, so allowing that address would not let you in.",
                    {
                        fam: access.person_family,
                        ip: access.person_ip,
                        fam_host: access.host_family,
                    }
                );
            default:
                return "";
        }
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
     * With "Also allow my IP" ticked, the address the box showed goes
     * along; the server adds it to the host's SSH list only if it is
     * still the asker's, and answers with the list as stored, which is
     * handed to ``onApplied`` for the page to keep.
     *
     * @param {boolean} chainFullSetup queue Full Setup after it
     */
    async _apply(chainFullSetup) {
        if (!this.canSubmit) return;
        this.state.submitting = true;
        try {
            const params = {
                host_id: this.props.hostId,
                chain_full_setup: chainFullSetup,
            };
            if (this.showAllowMyIp && this.state.allowMyIp) {
                params.allow_ip = this.sshAccess.person_ip;
            }
            const res = await rpc("/cloud/apply_host_hardening", params);
            if (res?.ok) {
                // The hardening, first of the chain when Full Setup
                // follows: started, or queued behind the host's job.
                await announceJob(this.env.toast, res.job_ids?.[0]);
                this.props.onApplied?.(res.job_ids || [], res.allowed_ssh_ips);
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
