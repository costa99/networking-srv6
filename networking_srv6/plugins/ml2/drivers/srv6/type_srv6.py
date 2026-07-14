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

"""ML2 type driver handing out SRv6 function IDs.

Under the chassis-locator SID model (docs/implementation-plan.md
section 1.1) each compute node owns a routable IPv6 locator and each
network gets a cloud-global *function ID* -- the End.DT4/DT6 function
within every chassis' locator, playing the role a VNI plays for
geneve/vxlan. Neutron's NetworkSegment.segmentation_id column is an
Integer, so the function ID is stored there directly (same pattern as
type_vlan/type_vxlan). The full SID only exists at send time:
build_sid(chassis_locator, segmentation_id) combines the *destination*
chassis' locator with the network's function ID; later phases (OVN
SB schema, End.DT4/DT6 programming) call it per remote chassis.

Allocation state lives in the srv6_locator_allocations table: one row
per allocatable function ID (1 .. 2^function_bits - 1), pre-populated
at initialize() (mirroring how type_tunnel syncs its ID ranges), with
the 'allocated' flag flipped race-safely by the SegmentTypeDriver
helper inside the network create/delete transaction.
"""

import netaddr
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins.ml2 import api
from oslo_config import cfg
from oslo_log import log

from neutron.plugins.ml2.drivers import helpers
from neutron.plugins.ml2.drivers.type_tunnel import chunks

from networking_srv6 import constants
from networking_srv6.db import objects as srv6_obj
from networking_srv6.plugins.ml2.drivers.srv6 import ovn_compat

LOG = log.getLogger(__name__)

srv6_opts = [
    cfg.StrOpt('locator_pool',
               default='fc00:0:1::/48',
               help='IPv6 prefix that per-chassis SRv6 locators are '
                    'carved out of.'),
    cfg.IntOpt('locator_prefix_length',
               default=64,
               help='Prefix length of each chassis locator carved from '
                    'locator_pool, e.g. 64 => fc00:0:1:<node>::/64. '
                    'Chassis locators themselves are operator-assigned '
                    'on each node; this is used for validation.'),
    cfg.IntOpt('function_bits',
               default=constants.DEFAULT_FUNCTION_BITS,
               help='Width in bits of the per-network function field '
                    'that follows the chassis locator in a SID, e.g. '
                    '16 => SID fc00:0:1:<node>:<fn>::/80 and up to '
                    '65535 networks.'),
]

cfg.CONF.register_opts(srv6_opts, 'ml2_type_srv6')

TYPE_SRV6 = constants.TYPE_SRV6

# Upper bound on pre-populated allocation rows; function_bits values
# that allow more function IDs than this are rejected at startup rather
# than flooding the table.
MAX_ALLOCATION_ROWS = 1 << 20

BULK_SIZE = 500


class SRv6TypeDriverError(n_exc.NeutronException):
    message = 'SRv6 type driver error: %(reason)s'


class SRv6TypeDriver(helpers.SegmentTypeDriver):
    """Allocates one IPv6 locator per network instead of a VNI."""

    def __init__(self):
        super().__init__(srv6_obj.SRv6LocatorAllocation)
        self.segmentation_key = next(iter(self.primary_keys))
        self.model_segmentation_id = self.model.get_segmentation_id()
        self._pool = netaddr.IPNetwork(cfg.CONF.ml2_type_srv6.locator_pool)
        self._prefix_length = cfg.CONF.ml2_type_srv6.locator_prefix_length
        self._function_bits = cfg.CONF.ml2_type_srv6.function_bits

    def get_type(self):
        return TYPE_SRV6

    def initialize(self):
        if self._prefix_length <= self._pool.prefixlen:
            LOG.error('locator_prefix_length (%s) must be longer than '
                      'the locator_pool prefix (%s). Service terminated!',
                      self._prefix_length, self._pool.prefixlen)
            raise SystemExit()
        if self._prefix_length + self._function_bits > 128:
            LOG.error('locator_prefix_length (%s) plus function_bits (%s) '
                      'exceeds the 128 bits of a SID. Service terminated!',
                      self._prefix_length, self._function_bits)
            raise SystemExit()
        if self._function_bits < 1 or self._max_offset() > MAX_ALLOCATION_ROWS:
            LOG.error('function_bits (%s) must allow between 1 and %s '
                      'function IDs. Service terminated!',
                      self._function_bits, MAX_ALLOCATION_ROWS)
            raise SystemExit()
        ovn_compat.allow_srv6_network_type()
        self._sync_allocations()
        LOG.info('SRv6TypeDriver initialized with locator_pool=%s '
                 'locator_prefix_length=%s function_bits=%s '
                 '(%s allocatable function IDs)',
                 self._pool, self._prefix_length, self._function_bits,
                 self._max_offset())

    def _max_offset(self):
        return (1 << self._function_bits) - 1

    @db_api.retry_db_errors
    def _sync_allocations(self):
        """Pre-populate one allocation row per offset in the pool.

        Same shape as type_tunnel._sync_allocations: fast-exit when the
        table already matches the config, drop unallocated rows that
        fell out of the pool after a reconfiguration, bulk-insert the
        missing ones. retry_db_errors absorbs the DBDuplicateEntry race
        when several API workers sync concurrently at startup: the
        loser retries and takes the fast-exit path.
        """
        offsets = set(range(1, self._max_offset() + 1))
        offset_col = self.model.get_segmentation_id()
        ctx = n_context.get_admin_context()
        with db_api.CONTEXT_WRITER.using(ctx):
            num_in_pool = ctx.session.query(self.model).filter(
                offset_col.in_(offsets)).count()
            num_total = ctx.session.query(self.model).count()
            if len(offsets) == num_in_pool == num_total:
                return

            allocs = ctx.session.query(self.model).all()

            unallocated = (a.locator_offset for a in allocs
                           if not a.allocated)
            to_remove = (x for x in unallocated if x not in offsets)
            for chunk in chunks(to_remove, BULK_SIZE):
                (ctx.session.query(self.model).
                 filter(offset_col.in_(chunk)).
                 filter_by(allocated=False).
                 delete(synchronize_session=False))

            existing = {a.locator_offset for a in allocs}
            missing = list(offsets - existing)
            for chunk in chunks(missing, BULK_SIZE):
                bulk = [{'locator_offset': x, 'allocated': False}
                        for x in chunk]
                ctx.session.execute(self.model.__table__.insert(), bulk)

    def initialize_network_segment_range_support(self, start_time):
        # The network-segment-range service plugin is not supported for
        # srv6 segments (the pool comes from ml2_type_srv6 config only).
        pass

    def update_network_segment_range_allocations(self):
        pass

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
                reason='segmentation_id %s outside function ID range '
                       '1..%s (function_bits=%s)' %
                (segmentation_id, self._max_offset(), self._function_bits))

    def reserve_provider_segment(self, context, segment, filters=None):
        if self.is_partial_segment(segment):
            filters = filters or {}
            alloc = self.allocate_partially_specified_segment(
                context, **filters)
            if not alloc:
                raise n_exc.NoNetworkAvailable()
        else:
            segmentation_id = segment.get(api.SEGMENTATION_ID)
            alloc = self.allocate_fully_specified_segment(
                context, **{self.segmentation_key: segmentation_id})
            if not alloc:
                raise SRv6TypeDriverError(
                    reason='locator offset %s already in use' %
                    segmentation_id)
        return self._segment_dict(getattr(alloc, self.segmentation_key))

    def allocate_tenant_segment(self, context, filters=None):
        filters = filters or {}
        alloc = self.allocate_partially_specified_segment(context, **filters)
        if not alloc:
            return
        offset = getattr(alloc, self.segmentation_key)
        LOG.debug('Allocated SRv6 function ID %s', offset)
        return self._segment_dict(offset)

    def release_segment(self, context, segment):
        offset = segment[api.SEGMENTATION_ID]
        inside = 1 <= offset <= self._max_offset()
        with db_api.CONTEXT_WRITER.using(context):
            query = (context.session.query(self.model).
                     filter_by(**{self.segmentation_key: offset}))
            if inside:
                count = query.update({'allocated': False})
            else:
                # Offset no longer in the configured range (function_bits
                # was shrunk while the network existed): drop the row.
                count = query.delete()
        if count:
            LOG.debug('Released SRv6 function ID %s', offset)
        else:
            LOG.warning('SRv6 function ID %s not found on release',
                        offset)

    @db_api.CONTEXT_READER
    def get_allocation(self, context, offset):
        return (context.session.query(self.model).
                filter_by(**{self.segmentation_key: offset}).
                first())

    def get_mtu(self, physical_network=None):
        # Same shape as type_tunnel.get_mtu(): min of the physnet/global
        # MTU and ml2.path_mtu, minus the encap overhead. Phases up to
        # 7.3.4 use reduced encap (single SID in the outer destination
        # address, no SRH), so the overhead is one IPv6 header.
        seg_mtu = super().get_mtu(physical_network)
        mtu = []
        if seg_mtu > 0:
            mtu.append(seg_mtu)
        if cfg.CONF.ml2.path_mtu > 0:
            mtu.append(cfg.CONF.ml2.path_mtu)
        return min(mtu) - constants.IPV6_HEADER_LEN if mtu else 0

    def get_function_id(self, segmentation_id):
        """Return the validated per-network SRv6 function ID."""
        if not 1 <= segmentation_id <= self._max_offset():
            raise SRv6TypeDriverError(
                reason='function ID %s outside range 1..%s '
                       '(function_bits=%s)' %
                (segmentation_id, self._max_offset(), self._function_bits))
        return segmentation_id

    def build_sid(self, chassis_locator, segmentation_id):
        """Combine a chassis locator with a network's function ID.

        Returns the End.DT4/DT6 SID (netaddr.IPNetwork) at which the
        given chassis decapsulates traffic for the given network, e.g.
        build_sid('fc00:0:1:2::/64', 5) -> fc00:0:1:2:5::/80.
        """
        locator = netaddr.IPNetwork(chassis_locator)
        if locator.version != 6:
            raise SRv6TypeDriverError(
                reason='chassis locator %s is not IPv6' % chassis_locator)
        if locator.prefixlen + self._function_bits > 128:
            raise SRv6TypeDriverError(
                reason='chassis locator %s too long for function_bits=%s' %
                (chassis_locator, self._function_bits))
        function_id = self.get_function_id(segmentation_id)
        shift = 128 - locator.prefixlen - self._function_bits
        address = netaddr.IPAddress(
            int(locator.network) + (function_id << shift), version=6)
        return netaddr.IPNetwork(
            '%s/%d' % (address, locator.prefixlen + self._function_bits))

    def _segment_dict(self, segmentation_id):
        return {api.NETWORK_TYPE: TYPE_SRV6,
                api.PHYSICAL_NETWORK: None,
                api.SEGMENTATION_ID: segmentation_id}
