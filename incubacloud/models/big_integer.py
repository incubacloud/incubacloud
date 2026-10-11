"""An integer field stored in a 64-bit column.

``fields.Integer`` always creates an ``int4`` column, whose largest value
is 2,147,483,647. Ids handed out by an outside service do not stop
there: GitHub's comment ids are past it, so storing the id of a pull
request preview's comment failed with "integer out of range" and the
preview ended in error (11-oct-2026).

``BigInteger`` behaves as ``fields.Integer`` in Python and stores a
``bigint``, up to 9,223,372,036,854,775,807. When a field moves to it,
Odoo converts the existing ``int4`` column in place on upgrade, without
losing a value::

    from .big_integer import BigInteger

    class MyModel(models.Model):
        comment_id = BigInteger(string="Comment ID")
"""
from odoo import fields


class BigInteger(fields.Integer):
    """``fields.Integer`` stored in a ``bigint`` column."""

    _column_type = ('int8', 'int8')
