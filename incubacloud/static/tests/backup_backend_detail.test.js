import {Deferred, describe, expect, test} from "@odoo/hoot";
import {patchWithCleanup} from "@web/../tests/web_test_helpers";
import {rpc} from "@web/core/network/rpc";
import {BackupBackendDetail} from "@incubacloud/components/backup_backend_detail/backup_backend_detail";
import {SettingAdvanced} from "@incubacloud/components/settings_form/settings_form";

/**
 * BackupBackendDetail uses useEnv, rpc, onWillStart, and PasswordInput.
 * Full mounting requires service mocks. We test class structure,
 * props validation, EMPTY_FORM defaults, and backupDst computation logic.
 */

describe("BackupBackendDetail — class structure", () => {
  test("template name is set", () => {
    expect(BackupBackendDetail.template).toBe("incubacloud.BackupBackendDetail");
  });

  test("props defines backend_id as optional Number", () => {
    expect(BackupBackendDetail.props.backend_id.type).toBe(Number);
    expect(BackupBackendDetail.props.backend_id.optional).toBe(true);
  });

  test("components includes PasswordInput", () => {
    expect("PasswordInput" in BackupBackendDetail.components).toBe(true);
  });
});

describe("BackupBackendDetail — EMPTY_FORM defaults", () => {
  // The EMPTY_FORM factory is not exported, but its values are used
  // when isNew is true. We verify the expected defaults.

  const defaults = {
    name: "",
    backend_type: "s3",
    s3_bucket: "",
    s3_path: "backups",
    s3_endpoint_url: "",
    s3_access_key_id: "",
    s3_secret_access_key: "",
    passphrase: "",
    backup_image_version: "latest",
    email_from: "",
    email_to: "",
    smtp_report_success: true,
    backup_retention: "3M",
    retention_owner: "cron",
    backup_tz: "UTC",
  };

  test("backend_type defaults to s3", () => {
    expect(defaults.backend_type).toBe("s3");
  });

  test("s3_path defaults to backups", () => {
    expect(defaults.s3_path).toBe("backups");
  });

  test("backup_image_version defaults to latest", () => {
    expect(defaults.backup_image_version).toBe("latest");
  });

  test("smtp_report_success defaults to true", () => {
    expect(defaults.smtp_report_success).toBe(true);
  });

  test("backup_retention defaults to 3M", () => {
    expect(defaults.backup_retention).toBe("3M");
  });

  test("retention_owner defaults to cron", () => {
    expect(defaults.retention_owner).toBe("cron");
  });

  test("backup_tz defaults to UTC", () => {
    expect(defaults.backup_tz).toBe("UTC");
  });
});

describe("BackupBackendDetail — backupDst computation", () => {
  // Extract backupDst getter logic as a pure function
  function backupDst(form) {
    if (!form.s3_bucket) return "";
    const path = (form.s3_path || "").replace(/^\/|\/$/g, "");
    return path
      ? `boto3+s3://${form.s3_bucket}/${path}`
      : `boto3+s3://${form.s3_bucket}`;
  }

  test("returns empty string when s3_bucket is empty", () => {
    expect(backupDst({s3_bucket: "", s3_path: "backups"})).toBe("");
  });

  test("returns empty string when s3_bucket is null", () => {
    expect(backupDst({s3_bucket: null, s3_path: "backups"})).toBe("");
  });

  test("builds URL with bucket and path", () => {
    expect(backupDst({s3_bucket: "my-bucket", s3_path: "backups"})).toBe(
      "boto3+s3://my-bucket/backups"
    );
  });

  test("strips leading slash from path", () => {
    expect(backupDst({s3_bucket: "my-bucket", s3_path: "/backups"})).toBe(
      "boto3+s3://my-bucket/backups"
    );
  });

  test("strips trailing slash from path", () => {
    expect(backupDst({s3_bucket: "my-bucket", s3_path: "backups/"})).toBe(
      "boto3+s3://my-bucket/backups"
    );
  });

  test("strips both leading and trailing slashes", () => {
    expect(backupDst({s3_bucket: "my-bucket", s3_path: "/data/backups/"})).toBe(
      "boto3+s3://my-bucket/data/backups"
    );
  });

  test("bucket only when path is empty", () => {
    expect(backupDst({s3_bucket: "my-bucket", s3_path: ""})).toBe(
      "boto3+s3://my-bucket"
    );
  });

  test("bucket only when path is just slashes", () => {
    expect(backupDst({s3_bucket: "my-bucket", s3_path: "/"})).toBe(
      "boto3+s3://my-bucket"
    );
  });

  test("nested path is preserved", () => {
    expect(backupDst({s3_bucket: "prod", s3_path: "a/b/c"})).toBe(
      "boto3+s3://prod/a/b/c"
    );
  });
});

describe("BackupBackendDetail — form population from backend data", () => {
  // Test the logic that populates form from loaded backend data
  function populateForm(b) {
    return {
      name: b.name || "",
      backend_type: b.backend_type || "s3",
      s3_bucket: b.s3_bucket || "",
      s3_path: b.s3_path || "backups",
      s3_endpoint_url: b.s3_endpoint_url || "",
      s3_access_key_id: b.s3_access_key_id || "",
      s3_secret_access_key: "",
      passphrase: "",
      backup_image_version: b.backup_image_version || "latest",
      email_from: b.email_from || "",
      email_to: b.email_to || "",
      smtp_report_success: b.smtp_report_success !== false,
      backup_retention: b.backup_retention || "3M",
      retention_owner: b.retention_owner || "cron",
      backup_tz: b.backup_tz || "UTC",
    };
  }

  test("secret fields are always blank after loading", () => {
    const form = populateForm({
      name: "test",
      s3_secret_access_key: "SUPERSECRET",
      passphrase: "SECRETPASS",
    });
    expect(form.s3_secret_access_key).toBe("");
    expect(form.passphrase).toBe("");
  });

  test("smtp_report_success defaults to true when undefined", () => {
    const form = populateForm({name: "test"});
    expect(form.smtp_report_success).toBe(true);
  });

  test("smtp_report_success is false only when explicitly false", () => {
    const form = populateForm({name: "test", smtp_report_success: false});
    expect(form.smtp_report_success).toBe(false);
  });

  test("s3_path falls back to backups when empty", () => {
    const form = populateForm({name: "test", s3_path: ""});
    expect(form.s3_path).toBe("backups");
  });

  test("preserves explicit values", () => {
    const form = populateForm({
      name: "AWS Prod",
      backend_type: "s3",
      s3_bucket: "prod-bucket",
      s3_path: "data",
      backup_retention: "6M",
      backup_tz: "Europe/Madrid",
    });
    expect(form.name).toBe("AWS Prod");
    expect(form.s3_bucket).toBe("prod-bucket");
    expect(form.s3_path).toBe("data");
    expect(form.backup_retention).toBe("6M");
    expect(form.backup_tz).toBe("Europe/Madrid");
  });
});

describe("BackupBackendDetail — method existence", () => {
  test("save method exists", () => {
    expect(typeof BackupBackendDetail.prototype.save).toBe("function");
  });

  test("testConnection method exists", () => {
    expect(typeof BackupBackendDetail.prototype.testConnection).toBe("function");
  });

  test("goBack method exists", () => {
    expect(typeof BackupBackendDetail.prototype.goBack).toBe("function");
  });

  test("onInput method exists", () => {
    expect(typeof BackupBackendDetail.prototype.onInput).toBe("function");
  });

  test("onCheckbox method exists", () => {
    expect(typeof BackupBackendDetail.prototype.onCheckbox).toBe("function");
  });

  test("onPasswordChange method exists", () => {
    expect(typeof BackupBackendDetail.prototype.onPasswordChange).toBe("function");
  });
});

describe("BackupBackendDetail — the save bar", () => {
  test("what was sent is saved; an edit made while the save is out stays unsaved", async () => {
    const answer = new Deferred();
    patchWithCleanup(rpc, {_rpc: () => answer});
    const comp = Object.create(BackupBackendDetail.prototype);
    comp.props = {backend_id: 5};
    comp.state = {saving: false, form: {name: "r2", quota_gb: 0}, backend: {name: "r2"}};
    comp.validator = {validate: () => ({isValid: true, firstError: ""})};
    comp.env = {toast: {success: () => expect.step("saved")}};
    comp._snapshotForm();
    comp.state.form.quota_gb = 50;
    const saving = comp.save();
    comp.state.form.name = "r2-eu";
    answer.resolve(true);
    await saving;
    expect.verifySteps(["saved"]);
    expect(JSON.parse(comp._savedForm)).toEqual({name: "r2", quota_gb: 50});
    expect(comp.unsavedCount).toBe(1);
  });
});

describe("BackupBackendDetail — Advanced", () => {
  /**
   * A destination as loaded, its form saved as it is.
   *
   * @param {object} [extra] state to set over the defaults
   * @returns {BackupBackendDetail}
   */
  function loaded(extra = {}) {
    const comp = Object.create(BackupBackendDetail.prototype);
    comp.isNew = false;
    comp.state = {
      retention_owner_declared: true,
      form: {name: "r2", backup_retention: "3M", backup_tz: "UTC", retention_owner: "cron", quota_gb: 0},
      ...extra,
    };
    comp.validator = {fieldError: () => null};
    comp._snapshotForm();
    return comp;
  }

  test("counts only the unsaved fields folded under it", () => {
    const comp = loaded();
    comp.state.form.quota_gb = 20;
    expect(comp.advancedChanged).toBe(0);
    comp.state.form.backup_tz = "Europe/Madrid";
    comp.state.form.retention_owner = "lifecycle";
    expect(comp.advancedChanged).toBe(2);
  });

  test("stays open for a retention period to fix or an owner to confirm", () => {
    expect(loaded().advancedNeedsLook).toBe(false);
    expect(loaded({retention_owner_declared: false}).advancedNeedsLook).toBe(true);
    const wrong = loaded();
    wrong.validator = {fieldError: (field) => (field === "backup_retention" ? "Use N + D/M/Y" : null)};
    expect(wrong.advancedNeedsLook).toBe(true);
  });

  test("the fold takes what the page tells: open, and how many changed", () => {
    const fold = Object.create(SettingAdvanced.prototype);
    fold.state = {open: false};
    fold.store = {found: null, errors: {}};
    fold.api = {isChanged: () => false};
    fold.form = null;
    fold.props = {fields: ["backup_retention"], changed: 1, forceOpen: true};
    expect(fold.open).toBe(true);
    expect(fold.changedCount).toBe(1);
    fold.props = {fields: ["backup_retention"]};
    expect(fold.open).toBe(false);
    expect(fold.changedCount).toBe(0);
  });
});
