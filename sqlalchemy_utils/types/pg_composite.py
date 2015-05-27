"""
CompositeType provides means to interact with
`PostgreSQL composite types`_. Currently this type features:

* Easy attribute access to composite type fields
* Supports SQLAlchemy TypeDecorator types
* Ability to include composite types as part of PostgreSQL arrays
* Type creation and dropping

Installation
^^^^^^^^^^^^

CompositeType automatically attaches `before_create` and `after_drop` DDL
listeners. These listeners create and drop the composite type in the
database. This means it works out of the box in your test environment where
you create the tables on each test run.

When you already have your database set up you should call
:func:`register_composites` after you've set up all models.

::

    register_composites(conn)



Usage
^^^^^

::

    from collections import OrderedDict

    import sqlalchemy as sa
    from sqlalchemy_utils import Composite, CurrencyType


    class Account(Base):
        __tablename__ = 'account'
        id = sa.Column(sa.Integer, primary_key=True)
        balance = sa.Column(
            CompositeType(
                'money_type',
                [
                    sa.Column('currency', CurrencyType),
                    sa.Column('amount', sa.Integer)
                ]
            )
        )


Accessing fields
^^^^^^^^^^^^^^^^

CompositeType provides attribute access to underlying fields. In the following
example we find all accounts with balance amount more than 5000.


::

    session.query(Account).filter(Account.balance.amount > 5000)


Arrays of composites
^^^^^^^^^^^^^^^^^^^^

::

    from sqlalchemy_utils import CompositeArray


    class Account(Base):
        __tablename__ = 'account'
        id = sa.Column(sa.Integer, primary_key=True)
        balances = sa.Column(
            CompositeArray(
                CompositeType(
                    'money_type',
                    [
                        sa.Column('currency', CurrencyType),
                        sa.Column('amount', sa.Integer)
                    ]
                )
            )
        )


.. _PostgreSQL composite types:
    http://www.postgresql.org/docs/devel/static/rowtypes.html


Related links:

http://schinckel.net/2014/09/24/using-postgres-composite-types-in-django/
"""
import psycopg2
import sqlalchemy as sa
from psycopg2.extensions import adapt, AsIs, register_adapter
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import _CreateDropBase
from sqlalchemy.sql.expression import FunctionElement
from sqlalchemy.types import (
    SchemaType,
    to_instance,
    TypeDecorator,
    UserDefinedType
)


class CompositeElement(FunctionElement):
    """
    Instances of this class wrap a Postgres composite type.
    """
    def __init__(self, base, field, type_):
        self.name = field
        self.type = to_instance(type_)

        super(CompositeElement, self).__init__(base)


@compiles(CompositeElement)
def _compile_pgelem(expr, compiler, **kw):
    return '(%s).%s' % (compiler.process(expr.clauses, **kw), expr.name)


class CompositeArray(ARRAY):
    def _proc_array(self, arr, itemproc, dim, collection):
        if dim is None:
            if issubclass(self.item_type.python_type, (list, tuple)):
                return arr
        ARRAY._proc_array(self, arr, itemproc, dim, collection)


# TODO: Make the registration work on connection level instead of global level
registered_composites = {}


class CompositeType(UserDefinedType, SchemaType):
    """
    Represents a PostgreSQL composite type.

    :param name:
        Name of the composite type.
    :param columns:
        List of columns that this composite type consists of
    """
    python_type = tuple

    class comparator_factory(UserDefinedType.Comparator):
        def __getattr__(self, key):
            try:
                type_ = self.type.typemap[key]
            except KeyError:
                raise KeyError(
                    "Type '%s' doesn't have an attribute: '%s'" % (
                        self.name, key
                    )
                )

            return CompositeElement(self.expr, key, type_)

    def __init__(self, name, columns):
        SchemaType.__init__(self)
        self.name = name
        self.columns = columns
        if name in registered_composites:
            self.type_cls = registered_composites[name].type_cls
        else:
            registered_composites[name] = self
        attach_composite_listeners()

    def get_col_spec(self):
        return self.name

    def result_processor(self, dialect, coltype):
        def process(value):
            cls = value.__class__
            kwargs = {}
            for column in self.columns:
                if isinstance(column.type, TypeDecorator):
                    kwargs[column.name] = column.type.process_result_value(
                        getattr(value, column.name), dialect
                    )
                else:
                    kwargs[column.name] = getattr(value, column.name)
            return cls(**kwargs)
        return process

    def create(self, bind=None, checkfirst=None):
        if (
            not checkfirst or
            not bind.dialect.has_type(bind, self.name, schema=self.schema)
        ):
            bind.execute(CreateCompositeType(self))

    def drop(self, bind=None, checkfirst=True):
        if (
            checkfirst and
            bind.dialect.has_type(bind, self.name, schema=self.schema)
        ):
            bind.execute(DropCompositeType(self))


def register_psycopg2_composite(dbapi_connection, composite):
    composite.type_cls = psycopg2.extras.register_composite(
        composite.name,
        dbapi_connection,
        globally=True
    ).type

    def adapt_composite(value):
        values = [
            adapt(getattr(value, column.name)).getquoted().decode('utf-8')
            for column in
            composite.columns
        ]
        return AsIs("(%s)::%s" % (', '.join(values), composite.name))

    register_adapter(composite.type_cls, adapt_composite)


def before_create(target, connection, **kw):
    for name, composite in registered_composites.items():
        composite.create(connection, checkfirst=True)
        register_psycopg2_composite(
            connection.connection.connection,
            composite
        )


def after_drop(target, connection, **kw):
    for name, composite in registered_composites.items():
        composite.drop(connection, checkfirst=True)


def register_composites(connection):
    for name, composite in registered_composites.items():
        register_psycopg2_composite(
            connection.connection.connection,
            composite
        )


def attach_composite_listeners():
    listeners = [
        (sa.MetaData, 'before_create', before_create),
        (sa.MetaData, 'after_drop', after_drop),
    ]
    for listener in listeners:
        if not sa.event.contains(*listener):
            sa.event.listen(*listener)


def remove_composite_listeners():
    listeners = [
        (sa.MetaData, 'before_create', before_create),
        (sa.MetaData, 'after_drop', after_drop),
    ]
    for listener in listeners:
        if sa.event.contains(*listener):
            sa.event.remove(*listener)


class CreateCompositeType(_CreateDropBase):
    pass


@compiles(CreateCompositeType)
def _visit_create_composite_type(create, compiler, **kw):
    type_ = create.element
    fields = ', '.join(
        '{name} {type}'.format(
            name=column.name,
            type=compiler.dialect.type_compiler.process(
                to_instance(column.type)
            )
        )
        for column in type_.columns
    )

    return 'CREATE TYPE {name} AS ({fields})'.format(
        name=compiler.preparer.format_type(type_),
        fields=fields
    )


class DropCompositeType(_CreateDropBase):
    pass


@compiles(DropCompositeType)
def _visit_drop_composite_type(drop, compiler, **kw):
    type_ = drop.element

    return 'DROP TYPE {name}'.format(name=compiler.preparer.format_type(type_))