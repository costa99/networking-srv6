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

TYPE_SRV6 = 'srv6'

# SID layout (see docs/implementation-plan.md section 1.1). With the
# default config the 128 SID bits split as:
#
#   | locator_pool /48 | node 16 bits | function 16 bits | zero |
#   fc00:0:1           : <node>       : <fn>             ::
#
# The chassis locator (pool + node bits, locator_prefix_length total) is
# operator-assigned per compute node; the function ID is what this
# driver allocates per network; the full SID is only formed at send time
# by build_sid(chassis_locator, function_id).
DEFAULT_FUNCTION_BITS = 16

# Encapsulation overhead in bytes. Reduced encap (a single SID carried
# in the outer destination address) costs just the outer IPv6 header;
# an explicit SRH adds its base header plus one slot per segment.
IPV6_HEADER_LEN = 40
SRH_BASE_LEN = 8
SRH_SEGMENT_LEN = 16
