/** Puts the continuous recovery panel at the top of the Backups tab. */
import { InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";
import { PitrPanel } from "./pitr_panel";

InstanceDetail.components = Object.assign(
    InstanceDetail.components || {}, { PitrPanel },
);
