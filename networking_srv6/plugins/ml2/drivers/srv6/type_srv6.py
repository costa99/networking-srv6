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

"""Phase 7.3.1 skeleton: ML2 type driver handing out SRv6 locators.

Neutron's NetworkSegment.segmentation_id column is an Integer, so this
driver keeps that column as a small integer offset (same pattern as
type_vlan/type_vni) and derives the actual IPv6 locator by combining
that offset with the configured locator_pool. get_locator() below is
the seam later phases (OVN NB/SB schema, End.DT4/DT6 programming) will
call to get the real IPv6 prefix for a segment.

Allocation state is currently in-memory only -- there is no backing DB
table/alembic migration yet. That is the first thing to add before
this is usable beyond a single neutron-server process.
"""

import netaddr
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins.ml2 import api
from oslo_config import cfg
from oslo_log import log

from networking_srv6.plugins.ml2.drivers.srv6 import ovn_compat

LOG = log.getLogger(__name__)

srv6_opts = [
    cfg.StrOpt('locator_pool',
               default='fc00:0:1::/48',
               help='IPv6 prefix that per-network SRv6 locators are '
                    'carved out of.'),
    cfg.IntOpt('locator_prefix_length',
               default=64,
               help='Prefix length handed out to each network as its '
                    'own locator, e.g. 64 => fc00:0:1:<offset>::/64.'),
]

cfg.CONF.register_opts(srv6_opts, 'ml2_type_srv6')

TYPE_SRV6 = 'srv6'


class SRv6TypeDriverError(n_exc.NeutronException):
    message = 'SRv6 type driver error: %(reason)s'


class SRv6TypeDriver(api.TypeDriver):
    """Allocates one IPv6 locator per network instead of a VNI."""

    def __init__(self):
        super().__init__()
        self._pool = netaddr.IPNetwork(cfg.CONF.ml2_type_srv6.locator_pool)
        self._prefix_length = cfg.CONF.ml2_type_srv6.locator_prefix_length
        # TODO(phase 7.3.1): replace with a real DB-backed allocation
        # table (alembic migration under networking_srv6/db/migration)
        # so allocations survive a neutron-server restart and are
        # visible across API workers.
        self._allocations = {}  # {offset: network_id or None}
        self._next_offset = 1

    def get_type(self):
        return TYPE_SRV6

    def initialize(self):
        ovn_compat.allow_srv6_network_type()
        LOG.info('SRv6TypeDriver initialized with locator_pool=%s '
                  'locator_prefix_length=%s',
                  self._pool, self._prefix_length)

    def is_partial_segment(self, segment):
        return segment.get(api.SEGMENTATION_ID) is None

    def validate_provider_segment(self, segment):
        if segment.get(api.PHYSICAL_NETWORK) is not None:
            raise SRv6TypeDriverError(
                reason='provider:physical_network is not valid for '
                       'network type %s' % TYPE_SRV6)
        segmentation_id = segment.get(api.SEGMENTATION_ID)
        if segmentation_id is None:
            return
        if not 1 <= segmentation_id <= self._max_offset():
            raise SRv6TypeDriverError(
                reason='segmentation_id %s outside locator offset range '
                       '1..%s for pool %s' %
                (segmentation_id, self._max_offset(), self._pool))

    def reserve_provider_segment(self, context, segment, filters=None):
        segmentation_id = segment.get(api.SEGMENTATION_ID)
        if segmentation_id is None:
            return self.allocate_tenant_segment(context, filters)
        if self._allocations.get(segmentation_id) is not None:
            raise SRv6TypeDriverError(
                reason='locator offset %s already in use' %
                segmentation_id)
        self._allocations[segmentation_id] = True
        return self._segment_dict(segmentation_id)

    def allocate_tenant_segment(self, context, filters=None):
        offset = self._next_offset
        self._next_offset += 1
        self._allocations[offset] = True
        LOG.debug('Allocated SRv6 locator offset=%s -> %s',
                   offset, self.get_locator(offset))
        return self._segment_dict(offset)

    def release_segment(self, context, segment):
        offset = segment.get(api.SEGMENTATION_ID)
        self._allocations.pop(offset, None)

    def get_mtu(self, physical_network=None):
        # IPv6 SRH adds an 8-byte base header plus 16 bytes per segment
        # to whatever the underlay MTU is; real accounting happens
        # once the underlay/encap path exists (phase 7.3.2+).
        return 0

    def _max_offset(self):
        return (1 << (self._prefix_length - self._pool.prefixlen)) - 1

    def get_locator(self, segmentation_id):
        """Return the IPv6 locator (netaddr.IPNetwork) for a segment."""
        block_size = 1 << (128 - self._prefix_length)
        base = int(self._pool.network)
        offset_addr = base + segmentation_id * block_size
        locator = netaddr.IPNetwork(
            '%s/%d' % (netaddr.IPAddress(offset_addr, version=6),
                        self._prefix_length))
        if offset_addr not in self._pool:
            raise SRv6TypeDriverError(
                reason='locator_pool %s exhausted at offset %s' %
                (self._pool, segmentation_id))
        return locator

    def _segment_dict(self, segmentation_id):
        return {api.NETWORK_TYPE: TYPE_SRV6,
                api.PHYSICAL_NETWORK: None,
                api.SEGMENTATION_ID: segmentation_id}
