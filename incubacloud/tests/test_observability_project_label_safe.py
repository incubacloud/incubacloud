"""A project's name reaches the observability playbook as data.

The host's scrape configuration labels each instance with its project's
name. The name is kept to one line of text without template delimiters,
travels to Ansible marked as not a template, and the playbook writes it
into the YAML file through ``to_json``, so it reads back as itself.
"""
import json
import re
from pathlib import Path

import jinja2
import yaml

from odoo.exceptions import ValidationError

from odoo.addons.incubacloud.models.observability_agents_executor import (
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)

_PLAYBOOK = (
    Path(__file__).resolve().parent.parent
    / "ansible" / "playbooks" / "host_observability.yml"
)


class TestProjectNameIsData(ObservabilityExecutorCase):

    def test_names_with_template_or_line_syntax_are_refused(self):
        """Template openers, line breaks and control characters."""
        project = self.env["cloud.project"].create({"name": "Fine"})
        for value in (
            "a {{ b }}",
            "a {% b %}",
            "a {# b #}",
            "a\nb",
            "a\tb",
            "a\u2028b",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                project.write({"name": value})
            with self.subTest(value=value, op="create"), \
                    self.assertRaises(ValidationError):
                self.env["cloud.project"].create({"name": value})

    def test_ordinary_names_are_kept(self):
        """Accents, quotes, a backslash and a single brace pass: the
        playbook writes them through ``to_json``."""
        name = 'Año (beta) — O\'Brien "B" a\\b {x} #1: ok'
        project = self.env["cloud.project"].create({"name": name})
        self.assertEqual(project.name, name)

    def _labelled_instance(self, project):
        """A deployed instance of *project* on the test's host."""
        instance = self.env["cloud.instance"].create({
            "name": "labelled", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        instance._transition("deploying")
        instance._transition("deployed")
        return instance

    def test_extra_vars_mark_the_project_label_as_not_a_template(self):
        """The label travels as Ansible's unsafe marker, the rest plain."""
        project = self.env["cloud.project"].create({"name": "Labels"})
        instance = self._labelled_instance(project)
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        label = next(
            i for i in extra["ic_instances"]
            if i["instance_id"] == str(instance.id)
        )
        self.assertEqual(label["project"], {"__ansible_unsafe": "Labels"})
        self.assertEqual(label["instance"], "labelled")

    def test_the_scrape_file_holds_the_label_as_one_scalar(self):
        """Rendered with ``to_json``, a value with YAML syntax stays one
        string of the file: it reads back as itself."""
        tasks = yaml.safe_load(_PLAYBOOK.read_text())[0]["tasks"]
        content = next(
            t["ansible.builtin.copy"]["content"] for t in tasks
            if t.get("ansible.builtin.copy", {}).get("dest", "").endswith(
                "/scrape.yml",
            )
        )
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters["to_json"] = lambda v, **kw: json.dumps(v, **kw)
        odd = 'x"\ny: [z]: #'
        # Every label the file writes through ``to_json`` holds the odd
        # value: the project's, and any other a module fills in.
        free = set(re.findall(r"inst\.(\w+) \| to_json", content))
        self.assertIn("project", free)
        instance = dict.fromkeys(re.findall(r"inst\.(\w+)", content), "")
        instance |= {
            "compose_project": "cp", "instance_id": "7",
            "instance": "inst", "traefik_prefix": "cp-19-0-",
            "traefik_extra": [],
        }
        instance |= dict.fromkeys(free, odd)
        rendered = env.from_string(content).render(
            ic_host_name="h", ic_host_id="1", ic_account="a",
            ic_has_traefik=True, ic_instances=[instance],
        )
        doc = yaml.safe_load(rendered)
        labels = [
            rule["replacement"]
            for job in doc["scrape_configs"]
            for rule in job.get("metric_relabel_configs") or []
            if rule["target_label"] in free
        ]
        self.assertEqual(labels, [odd] * content.count("| to_json"))
