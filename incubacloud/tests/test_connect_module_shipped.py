"""What a deploy ships of incubacloud_connect, for every series' image.

doodba compiles every addon of the image with the series' own Python:
2.7 on 7.0 to 10.0, 3.5 on 11.0 and 12.0. What reaches it from the panel
has to compile there, and the module's code has to import on the oldest
series it routes for (9.0, ``openerp``). The module's tests are the
panel's and stay behind (QA of the series, 7-nov-2026).
"""
import ast
import os
from pathlib import Path

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models import deploy_instance_executor as deploy
from odoo.addons.incubacloud.tests.test_executor_features import (
    _make_deploy_executor,
    _make_rebuild_executor,
)

#: What Python 2.7 or 3.5 lack, as the module would import it.
_TOO_NEW_IMPORTS = {"pathlib", "secrets", "contextlib.suppress"}


def _python_files(root):
    """Every ``.py`` file under *root*, relative to it, sorted."""
    return sorted(
        os.path.relpath(os.path.join(folder, name), root)
        for folder, _dirs, files in os.walk(root)
        for name in files if name.endswith(".py")
    )


class TestTheConnectModuleTheInstancesGet(BaseCase):
    """The copy ``_ic_connect_for_instances`` hands the upload."""

    def test_it_is_the_module_without_its_tests_or_bytecode(self):
        with deploy._ic_connect_for_instances() as module:
            self.assertEqual(os.path.basename(module), "incubacloud_connect")
            files = _python_files(module)
            every = [
                os.path.relpath(os.path.join(folder, name), module)
                for folder, _dirs, names in os.walk(module) for name in names
            ]
        self.assertIn("controllers/connect.py", files)
        self.assertIn("__manifest__.py", files)
        self.assertIn("__openerp__.py", files)
        self.assertFalse([f for f in every if f.startswith("tests")], every)
        self.assertFalse([f for f in every if f.endswith(".pyc")], every)
        self.assertFalse(Path(module).exists(), "the copy goes with the block")

    def test_nothing_in_it_needs_a_python_newer_than_the_series(self):
        """No f-string (3.6) and no import Python 2.7 or 3.5 lacks."""
        with deploy._ic_connect_for_instances() as module:
            for rel in _python_files(module):
                source = Path(module, rel).read_text(encoding="utf-8")
                with self.subTest(file=rel):
                    nodes = list(ast.walk(ast.parse(source)))
                    self.assertFalse(
                        [n.lineno for n in nodes if isinstance(n, ast.JoinedStr)],
                    )
                    imported = set()
                    for node in nodes:
                        if isinstance(node, ast.Import):
                            imported |= {alias.name for alias in node.names}
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            imported.add(node.module)
                            imported |= {
                                f"{node.module}.{alias.name}" for alias in node.names
                            }
                    self.assertFalse(imported & _TOO_NEW_IMPORTS, imported)

    def test_both_manifests_say_the_same(self):
        """9.0 reads ``__openerp__.py`` alone; every later series the other."""
        def read(name):
            path = Path(deploy._IC_CONNECT_MODULE, name)
            return ast.literal_eval(path.read_text(encoding="utf-8"))

        self.assertEqual(read("__openerp__.py"), read("__manifest__.py"))


class TestAnInstanceGetsOnlyTheNewCopy(BaseCase):
    """A deploy or a rebuild replaces the module an instance already has.

    ``cp -r`` over the old copy would keep what the new one no longer
    ships: the ``tests/`` of an instance deployed before 7-nov-2026, and
    with it the f-strings Python 2.7 and 3.5 refuse to compile.
    """

    def test_the_old_copy_goes_before_the_new_one_lands(self):
        target = "~/projects/test-inst/odoo/custom/src/private/incubacloud_connect"
        for make in (_make_deploy_executor, _make_rebuild_executor):
            with self.subTest(executor=make.__name__):
                step = next(
                    c[1] for c in make().get_commands()
                    if c[0] == "Install incubacloud_connect"
                )
                self.assertIn(f"rm -rf {target} && cp -r ", step)
