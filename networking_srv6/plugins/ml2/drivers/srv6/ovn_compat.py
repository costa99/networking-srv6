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

"""Temporary shim so the in-tree OVN mechanism driver accepts srv6.

OVNMechanismDriver hard-codes the network types it accepts and vetoes
everything else in create_network_precommit
(_validate_network_segments -> _is_network_type_supported), which
blocks creation of any srv6 network on an OVN deployment regardless of
what the type driver does.

Until the real OVN integration lands (phase 7.3.2+: NB/SB schema,
End.DT4/DT6 programming), wrap _is_network_type_supported so srv6
networks can be created. Delete this module when the mechanism-driver
phase starts.
"""

from oslo_log import log

LOG = log.getLogger(__name__)

_SRV6_TYPE = 'srv6'


def allow_srv6_network_type():
    """Patch OVNMechanismDriver to treat srv6 as a supported type."""
    try:
        from neutron.plugins.ml2.drivers.ovn.mech_driver import (
            mech_driver as ovn_mech)
    except ImportError:
        LOG.debug('OVN mechanism driver not importable; srv6 compat '
                  'shim not needed')
        return

    orig = getattr(ovn_mech.OVNMechanismDriver,
                   '_is_network_type_supported', None)
    if orig is None:
        LOG.warning('OVNMechanismDriver._is_network_type_supported not '
                    'found; srv6 networks will be rejected by the OVN '
                    'mechanism driver. Neutron internals may have '
                    'changed -- update ovn_compat.py.')
        return
    if getattr(orig, '_srv6_shim', False):
        return

    def _is_network_type_supported(self, network_type):
        return network_type == _SRV6_TYPE or orig(self, network_type)

    _is_network_type_supported._srv6_shim = True
    ovn_mech.OVNMechanismDriver._is_network_type_supported = (
        _is_network_type_supported)
    LOG.info('Patched OVNMechanismDriver to accept the srv6 network '
             'type (temporary shim until the SRv6 OVN integration '
             'lands)')
