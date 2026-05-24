import { Loader } from "@incubacloud/components/loader/loader";
import { getTemplate } from "@web/core/templates";
import { mount, reactive, whenReady } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { session } from "@web/session";
import { mountComponent } from "@web/env";
import { Chrome } from "@incubacloud/app/app";

const loader = reactive({ isShown: true });
whenReady(() => {
    mount(Loader, document.body, {
        getTemplate,
        props: { loader },
        translatableAttributes: ["data-tooltip"],
        translateFn: _t,
    });
});
(async function startCloudApp() {
    odoo.info = {
        db: session.db,
        server_version: session.server_version,
        server_version_info: session.server_version_info,
        isEnterprise: session.server_version_info.slice(-1)[0] === "e",
    };
    await whenReady();
    const app = await mountComponent(Chrome, document.body, {
        name: "Odoo IncubaCloud",
        props: { disableLoader: () => (loader.isShown = false) },
    });
})();
