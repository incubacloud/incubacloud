"""
Tier 2 — ORM integration tests for cloud.instance.domain.
"""

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestCloudInstanceDomainConstraint(TransactionCase):
    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "TestProj"})
        self.inst_a = self.env["cloud.instance"].create(
            {
                "name": "inst-a",
                "project_id": self.project.id,
                "environment": "production",
            }
        )
        self.inst_b = self.env["cloud.instance"].create(
            {
                "name": "inst-b",
                "project_id": self.project.id,
                "environment": "staging",
            }
        )

    def _domain(self, inst, hostname, **kw):
        return self.env["cloud.instance.domain"].create(
            {"instance_id": inst.id, "hostname": hostname} | kw
        )

    def test_unique_hostname_across_instances(self):
        """Same hostname on two different instances must raise."""
        self._domain(self.inst_a, "app.example.com")
        with self.assertRaises(ValidationError):
            self._domain(self.inst_b, "app.example.com")

    def test_same_instance_same_hostname_raises(self):
        """Duplicate hostname on the same instance must raise."""
        self._domain(self.inst_a, "app.example.com")
        with self.assertRaises(ValidationError):
            self._domain(self.inst_a, "app.example.com")

    def test_different_hostnames_allowed(self):
        d1 = self._domain(self.inst_a, "app.example.com")
        d2 = self._domain(self.inst_b, "other.example.com")
        self.assertTrue(d1.id)
        self.assertTrue(d2.id)

    def test_redirect_to_stored(self):
        d = self._domain(
            self.inst_a,
            "example.com",
            redirect_to="www.example.com",
        )
        self.assertEqual(d.redirect_to, "www.example.com")

    def test_default_cert_resolver(self):
        d = self._domain(self.inst_a, "secure.example.com")
        self.assertEqual(d.cert_resolver, "auto")

    def test_archived_instance_does_not_reserve_hostname(self):
        """An archived instance's domain must not block hostname reuse.

        Archiving is unrelated to the delete_instance job flow (which
        now either clears `deployed` and keeps the record, or unlinks
        it outright); this only covers the general `active=False`
        case, whatever sets it. A new instance must be able to reuse
        the same hostname.
        """
        self._domain(self.inst_a, "app.example.com")
        self.inst_a.active = False
        # Should not raise: the archived instance no longer reserves it.
        d = self._domain(self.inst_b, "app.example.com")
        self.assertTrue(d.id)

    def test_cascade_delete_with_instance(self):
        d = self._domain(self.inst_a, "temp.example.com")
        did = d.id
        self.inst_a.unlink()
        self.assertFalse(self.env["cloud.instance.domain"].browse(did).exists())


class TestCloudInstanceComputedDomain(TransactionCase):
    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "TestProj"})

    def _create_instance(self, **kw):
        base = {
            "name": "test",
            "project_id": self.project.id,
            "environment": "production",
        }
        return self.env["cloud.instance"].create(base | kw)

    def test_domain_computed_from_first_domain_id(self):
        inst = self._create_instance(
            domain_ids=[
                (0, 0, {"hostname": "primary.example.com", "sequence": 1}),
                (0, 0, {"hostname": "secondary.example.com", "sequence": 2}),
            ]
        )
        self.assertEqual(inst.domain, "primary.example.com")

    def test_domain_empty_when_no_domain_ids(self):
        inst = self._create_instance(domain_ids=[])
        self.assertFalse(inst.domain)

    def test_domain_updates_on_resequence(self):
        inst = self._create_instance(
            domain_ids=[
                (0, 0, {"hostname": "second.example.com", "sequence": 20}),
                (0, 0, {"hostname": "first.example.com", "sequence": 5}),
            ]
        )
        # Verify resequence changes the computed domain:
        inst.domain_ids.filtered(
            lambda d: d.hostname == "second.example.com"
        ).sequence = 1
        inst.invalidate_recordset()
        self.assertEqual(inst.domain, "second.example.com")


class TestDomainOrderPreserved(TransactionCase):
    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "TestProj"})
        self.inst = self.env["cloud.instance"].create(
            {
                "name": "dom-test",
                "project_id": self.project.id,
                "environment": "production",
            }
        )

    def test_ordering_by_sequence(self):
        Domain = self.env["cloud.instance.domain"]
        Domain.create(
            {
                "instance_id": self.inst.id,
                "hostname": "c.example.com",
                "sequence": 30,
            }
        )
        Domain.create(
            {
                "instance_id": self.inst.id,
                "hostname": "a.example.com",
                "sequence": 10,
            }
        )
        Domain.create(
            {
                "instance_id": self.inst.id,
                "hostname": "b.example.com",
                "sequence": 20,
            }
        )
        hostnames = Domain.search([("instance_id", "=", self.inst.id)]).mapped(
            "hostname"
        )
        self.assertEqual(
            hostnames,
            ["a.example.com", "b.example.com", "c.example.com"],
        )


class TestCertResolverDomain(TransactionCase):
    """``cert_resolver`` went from a free Char to a closed Selection.

    The value flows into the tenant's copier answers file, so an open
    domain was both a way to break a tenant's routing (by naming a
    resolver no host defines) and a needless injection surface.
    """

    def setUp(self):
        super().setUp()
        project = self.env["cloud.project"].create({"name": "CertProj"})
        self.inst = self.env["cloud.instance"].create({
            "name": "cert-inst",
            "project_id": project.id,
            "environment": "production",
        })
        self.Domain = self.env["cloud.instance.domain"]

    def _domain(self, **kw):
        return self.Domain.create(
            {"instance_id": self.inst.id, "hostname": "x.example.com"} | kw
        )

    def test_the_default_defers_to_the_host(self):
        """Whether a certificate authority can still reach a name is a
        property of the host, not something to maintain row by row."""
        self.assertEqual(self._domain().cert_resolver, "auto")

    def test_redirect_permanent_defaults_to_temporary(self):
        self.assertFalse(self._domain().redirect_permanent)

    def test_every_option_is_accepted(self):
        for value in ("auto", "letsencrypt", "custom", "none"):
            domain = self._domain(hostname=f"{value}.example.com",
                                  cert_resolver=value)
            self.assertEqual(domain.cert_resolver, value)

    def test_a_value_outside_the_selection_is_rejected(self):
        """An arbitrary resolver name must not reach the answers file."""
        with self.assertRaises(ValueError):
            self._domain(cert_resolver="acme-evil")

    def test_every_option_produces_an_answer(self):
        """Every option must produce a copier value, or a deploy crashes.

        Guards the pair: adding an option to the Selection without
        teaching ``_cert_resolver_answer`` about it would silently fall
        back to Let's Encrypt instead of doing what the option says.

        ``auto`` is the one option answered by asking the host rather
        than by looking it up, so it is the one option allowed to be
        missing from the table — and it is checked by calling.
        """
        options = set(dict(self.Domain._fields["cert_resolver"].selection))
        table = set(self.Domain._CERT_RESOLVER_ANSWERS)
        self.assertEqual(options - table, {"auto"})
        for value in options:
            domain = self._domain(
                hostname=f"answer-{value}.example.com", cert_resolver=value,
            )
            self.assertIn(
                domain._cert_resolver_answer(),
                ("letsencrypt", True, False),
            )
