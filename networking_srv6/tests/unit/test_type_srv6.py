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
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins.ml2 import api
from oslo_config import cfg

from networking_srv6.db import models
from networking_srv6.plugins.ml2.drivers.srv6 import type_srv6

# fc00:0:1::/48 carved into /56 locators -> offsets 1..255
TEST_POOL = 'fc00:0:1::/48'
TEST_PREFIX_LENGTH = 56
MAX_OFFSET = 255


class SRv6TypeDriverTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        ml2_config.register_ml2_plugin_opts()
        cfg.CONF.set_override('locator_pool', TEST_POOL,
                              group='ml2_type_srv6')
        cfg.CONF.set_override('locator_prefix_length', TEST_PREFIX_LENGTH,
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

    def test_allocate_tenant_segment_exhausted_pool(self):
        # /48 pool carved into /49 locators leaves a single offset.
        cfg.CONF.set_override('locator_prefix_length', 49,
                              group='ml2_type_srv6')
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

    def test_get_locator(self):
        self.assertEqual(netaddr.IPNetwork('fc00:0:1:100::/56'),
                         self.driver.get_locator(1))
        self.assertEqual(netaddr.IPNetwork('fc00:0:1:ff00::/56'),
                         self.driver.get_locator(MAX_OFFSET))

    def test_get_locator_outside_pool_raises(self):
        self.assertRaises(type_srv6.SRv6TypeDriverError,
                          self.driver.get_locator, MAX_OFFSET + 1)

    def test_allocate_tenant_segment_offset_maps_to_locator(self):
        segment = self.driver.allocate_tenant_segment(self.context)
        locator = self.driver.get_locator(segment[api.SEGMENTATION_ID])
        self.assertEqual(TEST_PREFIX_LENGTH, locator.prefixlen)
        self.assertIn(locator.network, netaddr.IPNetwork(TEST_POOL))

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

    def test_get_mtu_is_zero_until_encap_exists(self):
        self.assertEqual(0, self.driver.get_mtu())


class SRv6PoolReconfigurationTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        ml2_config.register_ml2_plugin_opts()
        cfg.CONF.set_override('locator_pool', TEST_POOL,
                              group='ml2_type_srv6')
        self.context = context.get_admin_context()

    def _driver(self, prefix_length):
        cfg.CONF.set_override('locator_prefix_length', prefix_length,
                              group='ml2_type_srv6')
        driver = type_srv6.SRv6TypeDriver()
        driver._sync_allocations()
        return driver

    def _rows(self, **filters):
        with self.context.session.begin():
            return (self.context.session.query(models.SRv6LocatorAllocation).
                    filter_by(**filters).count())

    def test_shrinking_pool_drops_unallocated_rows(self):
        self._driver(56)
        self.assertEqual(255, self._rows())
        self._driver(52)   # 15 offsets
        self.assertEqual(15, self._rows())

    def test_shrinking_pool_keeps_allocated_rows(self):
        driver = self._driver(56)
        segment = {api.NETWORK_TYPE: type_srv6.TYPE_SRV6,
                   api.PHYSICAL_NETWORK: None,
                   api.SEGMENTATION_ID: 200}
        driver.reserve_provider_segment(self.context, segment)
        driver = self._driver(52)   # offset 200 now outside the pool
        self.assertEqual(16, self._rows())   # 15 + the allocated leftover
        self.assertTrue(driver.get_allocation(self.context, 200).allocated)
        # releasing an out-of-pool offset deletes its row entirely
        driver.release_segment(self.context, segment)
        self.assertEqual(15, self._rows())
