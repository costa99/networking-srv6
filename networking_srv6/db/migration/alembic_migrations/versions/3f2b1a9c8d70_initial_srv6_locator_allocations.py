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

"""initial srv6_locator_allocations table

Revision ID: 3f2b1a9c8d70
Revises: None
Create Date: 2026-07-14

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '3f2b1a9c8d70'
down_revision = None


def upgrade():
    op.create_table(
        'srv6_locator_allocations',
        sa.Column('locator_offset', sa.Integer(), autoincrement=False,
                  nullable=False),
        sa.Column('allocated', sa.Boolean(), nullable=False,
                  server_default=sa.sql.false()),
        sa.PrimaryKeyConstraint('locator_offset'))
    op.create_index(op.f('ix_srv6_locator_allocations_allocated'),
                    'srv6_locator_allocations', ['allocated'])
