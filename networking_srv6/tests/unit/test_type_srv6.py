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

import netaddr
from neutron.conf.plugins.ml2 import config as ml2_config
from neutron.tests.unit import testlib_api
from neutron_lib import context
from neutron_lib.plugins.ml2 import api
from oslo_config import cfg

from networking_srv6.db import models
from networking_srv6.plugins.ml2.drivers.srv6 import type_srv6

TEST_POOL = 'fc00:0:1::/48'
TEST_PREFIX_LENGTH = 64      # chassis locators are /64s out of the pool
TEST_FUNCTION_BITS = 8       # function IDs 1..255
MAX_OFFSET = 255


class SRv6TypeDriverTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        ml2_config.register_ml2_plugin_opts()
        cfg.CONF.set_override('locator_pool', TEST_POOL,
                              group='ml2_type_srv6')
        cfg.CONF.set_override('locator_prefix_length', TEST_PREFIX_LENGTH,
                              group='ml2_type_srv6')
        cfg.CONF.set_override('function_bits', TEST_FUNCTION_BITS,
                              group='ml2_type_srv6')
        self.driver = type_srv6.SRv6TypeDriver()
        self.driver._sync_allocations()
        self.context = context.get_admin_context()

    def _rows(self, **filters):
        with self.context.session.begin():
            return (self.context.session.query(models.SRv6LocatorAllocation).
                    filter_by(**filters).count())

    def test_sync_allocations_populates_pool(self):
        self.assertEqual(MAX_OFFSET, self._rows())
        self.assertEqual(0, self._rows(allocated=True))

    def test_sync_allocations_is_idempotent(self):
        self.driver._sync_allocations()
        self.assertEqual(MAX_OFFSET, self._rows())

    def test_allocate_tenant_segment(self):
        segment = self.driver.allocate_tenant_segment(self.context)
        self.assertEqual(type_srv6.TYPE_SRV6, segment[api.NETWORK_TYPE])
        self.assertIsNone(segment[api.PHYSICAL_NETWORK])
        offset = segment[api.SEGMENTATION_ID]
        self.assertTrue(1 <= offset <= MAX_OFFSET)
        self.assertEqual(1, self._rows(allocated=True))

    def test_allocate_tenant_segment_exhausted_range(self):
        # function_bits=1 leaves a single allocatable function ID.
        cfg.CONF.set_override('function_bits', 1, group='ml2_type_srv6')
        driver = type_srv6.SRv6TypeDriver()
        driver._sync_allocations()
        self.assertIsNotNone(driver.allocate_tenant_segment(self.context))
        self.assertIsNone(driver.allocate_tenant_segment(self.context))

    def test_reserve_provider_segment_specific(self):
        segment = {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                   api.PHYSICAL_NETWORK: None,
                   api.SEGMENTATION_ID: 42}
        observed = self.driver.reserve_provider_segment(self.context, segment)
        self.assertEqual(42, observed[api.SEGMENTATION_ID])
        self.assertRaises(type_srv6.SRv6TypeDriverError,
                          self.driver.reserve_provider_segment,
                          self.context, segment)

    def test_reserve_provider_segment_partial(self):
        segment = {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                   api.PHYSICAL_NETWORK: None,
                   api.SEGMENTATION_ID: None}
        observed = self.driver.reserve_provider_segment(self.context, segment)
        self.assertTrue(1 <= observed[api.SEGMENTATION_ID] <= MAX_OFFSET)

    def test_release_segment_returns_offset_to_pool(self):
        segment = {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                   api.PHYSICAL_NETWORK: None,
                   api.SEGMENTATION_ID: 42}
        self.driver.reserve_provider_segment(self.context, segment)
        self.driver.release_segment(self.context, segment)
        self.assertEqual(0, self._rows(allocated=True))
        self.assertEqual(MAX_OFFSET, self._rows())
        # offset can be reserved again after release
        observed = self.driver.reserve_provider_segment(self.context, segment)
        self.assertEqual(42, observed[api.SEGMENTATION_ID])

    def test_release_segment_unknown_offset_does_not_raise(self):
        segment = {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                   api.PHYSICAL_NETWORK: None,
                   api.SEGMENTATION_ID: 9999}
        self.driver.release_segment(self.context, segment)
        self.assertEqual(MAX_OFFSET, self._rows())

    def test_is_partial_segment(self):
        self.assertTrue(self.driver.is_partial_segment(
            {api.SEGMENTATION_ID: None}))
        self.assertFalse(self.driver.is_partial_segment(
            {api.SEGMENTATION_ID: 42}))

    def test_validate_provider_segment_rejects_out_of_range(self):
        for bad_offset in (0, MAX_OFFSET + 1, -1):
            self.assertRaises(
                type_srv6.SRv6TypeDriverError,
                self.driver.validate_provider_segment,
                {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                 api.PHYSICAL_NETWORK: None,
                 api.SEGMENTATION_ID: bad_offset})

    def test_validate_provider_segment_rejects_physical_network(self):
        self.assertRaises(
            type_srv6.SRv6TypeDriverError,
            self.driver.validate_provider_segment,
            {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
             api.PHYSICAL_NETWORK: 'physnet1',
             api.SEGMENTATION_ID: 42})

    def test_validate_provider_segment_accepts_partial(self):
        self.driver.validate_provider_segment(
            {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
             api.PHYSICAL_NETWORK: None,
             api.SEGMENTATION_ID: None})

    def test_get_function_id(self):
        self.assertEqual(1, self.driver.get_function_id(1))
        self.assertEqual(MAX_OFFSET, self.driver.get_function_id(MAX_OFFSET))

    def test_get_function_id_out_of_range_raises(self):
        for bad in (0, MAX_OFFSET + 1, -1):
            self.assertRaises(type_srv6.SRv6TypeDriverError,
                              self.driver.get_function_id, bad)

    def test_get_allocation(self):
        segment = {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                   api.PHYSICAL_NETWORK: None,
                   api.SEGMENTATION_ID: 42}
        self.driver.reserve_provider_segment(self.context, segment)
        alloc = self.driver.get_allocation(self.context, 42)
        self.assertTrue(alloc.allocated)
        self.assertEqual(42, alloc.segmentation_id)

    def test_get_type(self):
        self.assertEqual('srv6', self.driver.get_type())


class SRv6BuildSidTestCase(testlib_api.SqlTestCase):
    """SID formation math, at the default 16-bit function width."""

    def setUp(self):
        super().setUp()
        ml2_config.register_ml2_plugin_opts()
        self.driver = type_srv6.SRv6TypeDriver()

    def test_build_sid_layout_example(self):
        # The reference example from docs/implementation-plan.md 1.1
        self.assertEqual(netaddr.IPNetwork('fc00:0:1:2:5::/80'),
                         self.driver.build_sid('fc00:0:1:2::/64', 5))

    def test_build_sid_accepts_ipnetwork_locator(self):
        locator = netaddr.IPNetwork('fc00:0:1:2::/64')
        self.assertEqual(netaddr.IPNetwork('fc00:0:1:2:5::/80'),
                         self.driver.build_sid(locator, 5))

    def test_build_sid_function_boundary(self):
        self.assertEqual(netaddr.IPNetwork('fc00:0:1:2:ffff::/80'),
                         self.driver.build_sid('fc00:0:1:2::/64', 0xffff))
        self.assertRaises(type_srv6.SRv6TypeDriverError,
                          self.driver.build_sid,
                          'fc00:0:1:2::/64', 0x10000)

    def test_build_sid_locator_too_long_raises(self):
        # 120 + 16 function bits > 128
        self.assertRaises(type_srv6.SRv6TypeDriverError,
                          self.driver.build_sid, 'fc00::/120', 5)

    def test_build_sid_rejects_non_ipv6_locator(self):
        self.assertRaises(type_srv6.SRv6TypeDriverError,
                          self.driver.build_sid, '10.0.0.0/24', 5)


class SRv6MTUTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        ml2_config.register_ml2_plugin_opts()
        self.driver = type_srv6.SRv6TypeDriver()

    def test_get_mtu_from_path_mtu(self):
        cfg.CONF.set_override('path_mtu', 1500, group='ml2')
        cfg.CONF.set_override('global_physnet_mtu', 9000)
        # min(9000, 1500) - 40 outer IPv6 header (reduced encap)
        self.assertEqual(1460, self.driver.get_mtu())

    def test_get_mtu_from_global_physnet_mtu(self):
        cfg.CONF.set_override('path_mtu', 0, group='ml2')
        cfg.CONF.set_override('global_physnet_mtu', 9000)
        self.assertEqual(8960, self.driver.get_mtu())

    def test_get_mtu_zero_when_unconfigured(self):
        cfg.CONF.set_override('path_mtu', 0, group='ml2')
        cfg.CONF.set_override('global_physnet_mtu', 0)
        self.assertEqual(0, self.driver.get_mtu())


class SRv6InitializeValidationTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        ml2_config.register_ml2_plugin_opts()
        cfg.CONF.set_override('locator_pool', TEST_POOL,
                              group='ml2_type_srv6')

    def _initialize(self, prefix_length, function_bits):
        cfg.CONF.set_override('locator_prefix_length', prefix_length,
                              group='ml2_type_srv6')
        cfg.CONF.set_override('function_bits', function_bits,
                              group='ml2_type_srv6')
        type_srv6.SRv6TypeDriver().initialize()

    def test_prefix_length_not_longer_than_pool_exits(self):
        self.assertRaises(SystemExit, self._initialize, 48, 16)

    def test_prefix_length_plus_function_bits_over_128_exits(self):
        self.assertRaises(SystemExit, self._initialize, 120, 16)

    def test_function_bits_above_row_cap_exits(self):
        self.assertRaises(SystemExit, self._initialize, 64, 21)

    def test_function_bits_below_one_exits(self):
        self.assertRaises(SystemExit, self._initialize, 64, 0)


class SRv6RangeReconfigurationTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        ml2_config.register_ml2_plugin_opts()
        cfg.CONF.set_override('locator_pool', TEST_POOL,
                              group='ml2_type_srv6')
        self.context = context.get_admin_context()

    def _driver(self, function_bits):
        cfg.CONF.set_override('function_bits', function_bits,
                              group='ml2_type_srv6')
        driver = type_srv6.SRv6TypeDriver()
        driver._sync_allocations()
        return driver

    def _rows(self, **filters):
        with self.context.session.begin():
            return (self.context.session.query(models.SRv6LocatorAllocation).
                    filter_by(**filters).count())

    def test_shrinking_range_drops_unallocated_rows(self):
        self._driver(8)
        self.assertEqual(255, self._rows())
        self._driver(4)   # 15 function IDs
        self.assertEqual(15, self._rows())

    def test_shrinking_range_keeps_allocated_rows(self):
        driver = self._driver(8)
        segment = {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                   api.PHYSICAL_NETWORK: None,
                   api.SEGMENTATION_ID: 200}
        driver.reserve_provider_segment(self.context, segment)
        driver = self._driver(4)   # function ID 200 now out of range
        self.assertEqual(16, self._rows())   # 15 + the allocated leftover
        self.assertTrue(driver.get_allocation(self.context, 200).allocated)
        # releasing an out-of-range function ID deletes its row entirely
        driver.release_segment(self.context, segment)
        self.assertEqual(15, self._rows())
