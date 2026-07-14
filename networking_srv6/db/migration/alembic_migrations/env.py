# Copyright 2026 costa99
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
#    implied. See the License for the specific language governing
#    permissions and limitations under the License.

from alembic import context
from neutron_lib.db import model_base
import sqlalchemy as sa
from sqlalchemy import event  # noqa

from neutron.db.migration import autogen
from neutron.db.migration.connection import DBConnection
from oslo_config import cfg

from networking_srv6.db import models  # noqa

MYSQL_ENGINE = None
# Separate version table so this subproject's alembic state never
# collides with neutron's own 'alembic_version' in the shared DB.
SRV6_VERSION_TABLE = 'alembic_version_srv6'

config = context.config
neutron_config = config.neutron_config

target_metadata = model_base.BASEV2.metadata


def set_mysql_engine():
    try:
        mysql_engine = neutron_config.command.mysql_engine
    except cfg.NoSuchOptError:
        mysql_engine = None

    global MYSQL_ENGINE
    MYSQL_ENGINE = (mysql_engine or
                    model_base.BASEV2.__table_args__['mysql_engine'])


@event.listens_for(sa.Table, 'after_parent_attach')
def set_storage_engine(target, parent):
    if MYSQL_ENGINE:
        target.kwargs['mysql_engine'] = MYSQL_ENGINE


def run_migrations_offline():
    set_mysql_engine()
    kwargs = dict()
    if neutron_config.database.connection:
        kwargs['url'] = neutron_config.database.connection
    else:
        kwargs['dialect_name'] = neutron_config.database.engine
    kwargs['version_table'] = SRV6_VERSION_TABLE
    context.configure(**kwargs)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    set_mysql_engine()
    connection = config.attributes.get('connection')
    with DBConnection(neutron_config.database.connection, connection) as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            version_table=SRV6_VERSION_TABLE,
            process_revision_directives=autogen.process_revision_directives)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
