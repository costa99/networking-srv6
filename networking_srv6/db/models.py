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

from neutron_lib.db import model_base
import sqlalchemy as sa
from sqlalchemy import sql


class SRv6LocatorAllocation(model_base.BASEV2):
    """One row per allocatable locator offset in the configured pool.

    Mirrors ml2_vxlan_allocations: rows are pre-populated from
    locator_pool/locator_prefix_length when the type driver initializes,
    and 'allocated' flips inside the network create/delete transaction.
    """

    __tablename__ = 'srv6_locator_allocations'

    locator_offset = sa.Column(sa.Integer, nullable=False, primary_key=True,
                               autoincrement=False)
    allocated = sa.Column(sa.Boolean, nullable=False, default=False,
                          server_default=sql.false(), index=True)

    @classmethod
    def get_segmentation_id(cls):
        return cls.locator_offset

    @property
    def segmentation_id(self):
        return self.locator_offset

    @staticmethod
    def primary_keys():
        return {'locator_offset'}
