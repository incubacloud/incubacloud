import { describe, expect, test } from "@odoo/hoot";
import { InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";

/**
 * The domain row's certificate picker. "auto" is the model's default and
 * what every stored row holds; a picker without it showed those rows as
 * Let's Encrypt and created new rows as Let's Encrypt, skipping the
 * host's own decision.
 */

/** Read the `certResolverOptions` getter without mounting the component. */
function certOptions() {
    return Object.getOwnPropertyDescriptor(
        InstanceDetail.prototype,
        "certResolverOptions"
    ).get.call({});
}

describe("InstanceDetail — certificate picker", () => {
    test("offers every model value, automatic first", () => {
        expect(certOptions().map((o) => o.value)).toEqual([
            "auto",
            "letsencrypt",
            "custom",
            "none",
        ]);
    });

    test("every option carries a label", () => {
        for (const option of certOptions()) {
            expect(Boolean(option.label)).toBe(true);
        }
    });

    test("a new domain row defers to the host", () => {
        const form = { domains: [] };
        InstanceDetail.prototype.addDomain.call({ state: { form } });
        expect(form.domains[0].cert_resolver).toBe("auto");
    });
});
