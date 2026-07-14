# Code map — network create to wire (phase 7.3.2 study)

Traced 2026-07-14 on the live devstack (neutron stable/2026.1, OVN 24.03
packaged, OVN main + OVS 3.3 source clone at `/opt/stack/ovn`). Line
numbers refer to those trees.

## Neutron: `openstack network create` -> NB

1. **API -> ML2.** `Ml2Plugin.create_network` -> `TypeManager`
   allocates the segment: our
   `SRv6TypeDriver.allocate_tenant_segment()` flips a row in
   `srv6_locator_allocations`; `segmentation_id` = function ID.
2. **Mech driver.** `OVNMechanismDriver.create_network_precommit`
   validates the type (`_is_network_type_supported`,
   `mech_driver.py` — currently shimmed by our `ovn_compat.py`),
   `create_network_postcommit` -> `OVNClient.create_network`.
3. **NB write.** `OVNClient.create_network`
   (`ovn_client.py:2187`) -> `_gen_network_parameters`
   (`ovn_client.py:2148`) builds the `Logical_Switch` row:
   - `external_ids["neutron:provnet-network-type"]` = `srv6`
     (`OVN_NETTYPE_EXT_ID_KEY`, written for *every* network type);
   - `external_ids["neutron:provnet-physical-network"]` only if physnet;
   - `other_config` = mcast/vlan-passthru only.

   **The function ID (segmentation_id) is NOT written to NB.**
   Verified live: `srv6-demo` (fn 31936) has no trace of 31936 anywhere
   in NB. `_gen_network_parameters` is the seam where phase 3's
   `SRv6OVNClient` override adds `neutron:srv6-function`.

## northd: NB -> SB, and the tunnel_key surprise

`Datapath_Binding` for `srv6-demo` has `tunnel_key=5` — allocated by
**northd**, sequentially, unrelated to neutron's segmentation_id. The
on-wire datapath identifier for geneve today is northd's tunnel_key,
not neutron's VNI. Consequence for the plan's open question #2:

- Option A (northd-native): use `tunnel_key` as the SRv6 function ID.
  Fits `function_bits=16` for < 65k datapaths, needs zero Neutron->OVN
  plumbing, but then Neutron's allocated fn is dead weight and the SID
  is not predictable from the Neutron API.
- Option B (neutron-driven): `physical.c`/northd read
  `neutron:srv6-function` from `Logical_Switch` external_ids (copied by
  northd into `Datapath_Binding.external_ids` — it already copies
  selected keys, see `northd.c` `join_datapaths`). Predictable SIDs,
  small mechanical northd change. **Recommended.**

## ovn-controller: SB -> tunnel ports and flows

- `controller/chassis.c` — turns `Open_vSwitch external_ids`
  (`ovn-encap-type`, `ovn-encap-ip`) into SB `Chassis`/`Encap` rows.
  Pattern to copy for the locator: `get_evpn_vxlan_port()`
  (`chassis.c:209`) reads an arbitrary external_id into chassis config;
  our `ovn-srv6-locator` follows the same shape, landing in the srv6
  `Encap.options`.
- `controller/encaps.c` — `encaps_run()` (`encaps.c:729`) walks remote
  chassis' `Encap` rows and creates OVS tunnel interfaces
  (`tunnel_add`); encap preference via `get_tunnel_type()`
  (`encaps.c:365,410`). This is where a remote srv6 Encap must become
  an OVS `type=srv6` port. Warm-up VLOG patch lives at the top of
  `encaps_run` (verified in sandbox).
- `controller/physical.c` — output-to-remote-chassis flows; sets
  `tun_id`/metadata and picks the tunnel ofport. The SID-forming change
  (dest = remote locator + fn) goes here. *Not yet read in detail —
  days 8–9.*

## OVS: the srv6 tunnel primitive (source-verified, not yet exercised)

- Port type registered: `netdev-vport.c:483` (`type == "srv6"`).
- Options: `remote_ip` (= final SID), `local_ip`,
  `srv6_segs` (comma-separated, **max 6 segments**, parsed by
  `parse_srv6_segs`, `netdev-vport.c:438`), `srv6_flowlabel`
  (`zero|compute|copy`, `netdev-vport.c:799`).
- Encap/decap implementation: `netdev-native-tnl.c` —
  `netdev_srv6_build_header` (:946), `netdev_srv6_push_header` (:1011),
  `netdev_srv6_pop_header` (:1047). These are **userspace (netdev)
  datapath** functions.
- **Kernel datapath: NOT supported.** `dpif-netlink-rtnl.c:133` maps
  `OVS_VPORT_TYPE_SRV6` to rtnl kind `"srv6"`, which does not exist in
  mainline Linux (`ip link add type srv6` -> "Unknown device type",
  verified on kernel 6.8). Bridges carrying srv6 ports must be
  `datapath_type=netdev`.

  Phase-3 design impact (for the design note): br-int on a stock
  devstack chassis is kernel-datapath. Options to evaluate days 6–7:
  (a) netdev br-int (userspace forwarding for all tenant traffic),
  (b) a separate netdev tunnel bridge patched to system br-int,
  (c) kernel `seg6`/`seg6local` routes outside OVS (descope-gate
  fallback in implementation-plan §5).

- Open question #1 (per-flow segment lists) is *partially* answered
  from source: `srv6_segs` is per-port config, and `remote_ip` can be
  `flow` like other tunnels — whether the full segment list can vary
  per flow needs the hands-on.

## Environment facts (verified on the box)

- OVS 3.3.4 advertises `srv6` in `iface_types`; OVN 24.03.6 packaged.
- Kernel 6.8: `seg6 encap` and `seg6local End` routes install fine;
  `net.ipv6.conf.all.seg6_enabled=0` by default (must be 1 on ingress
  interfaces for kernel-side SRH processing).
- OVN main + OVS submodule build clean (`/opt/stack/ovn`,
  `make -j4`, log `/tmp/ovn-build.log`).
- `make sandbox` works but its SSL PKI is broken here
  (`SSL_ERROR_ZERO_RETURN` from ovn-controller): fix inside the
  sandbox shell with
  `ovn-sbctl set-connection role=ovn-controller ptcp:6642` +
  `ovs-vsctl set Open_vSwitch . external_ids:ovn-remote=tcp:127.0.0.1:6642`.

## Hands-on results (days 6–7, run 2026-07-14)

OVS's own srv6 system tests are the canonical topology: OVS srv6 port
on one side, kernel seg6 (`End.DX4` + `encap seg6` routes) in a netns
on the other — passing them also proves OVS<->kernel wire-format
interop (the phase-5 FRR prerequisite).

- `make check-system-userspace TESTSUITEFLAGS='-k srv6'` in
  `/opt/stack/ovn/ovs`: **all pass** (needs `net-tools` for the legacy
  `arp` command).
- **Multi-segment SRH works**, proven by a new system test
  (`docs/ovn-patches/0001-...patch`): OVS encapsulates with
  `SRH [fc00:a::1, fc00:b::1] segleft=1`, outer DA = waypoint; kernel
  `End` at the waypoint advances to the final SID; ping 3/3. Wire
  capture: `docs/evidence/srv6-multiseg-waypoint.pcap`
  (`RT6 len=4, segleft=1, last-entry=1`).
- **Upstream OVS bug found and fixed** (in the same patch):
  `parse_srv6_segs()` runs `strtok_r` in place on the smap value owned
  by the config layer, so the first reconfigure truncates `srv6_segs`
  to its first segment — silently, no log. One-line fix: parse an
  `xstrdup` copy. Without the fix any multi-segment config degrades to
  single-SID after a bridge reconfigure. **Upstream-worthy.**
- **RX matching gotcha:** userspace tunnels match incoming packets by
  (local, remote) outer pair. With `srv6_segs` via a waypoint, the
  return traffic must arrive with outer src == the port's `remote_ip`
  (= the first segment), or use `remote_ip=flow`. Asymmetric paths need
  care in `encaps.c`/`physical.c`.
- **Per-flow SIDs confirmed possible in principle:**
  `netdev_srv6_build_header` validates `segs[0] == flow->tunnel.ipv6_dst`
  and falls back to the flow's tunnel dst when no segs are configured —
  so `remote_ip=flow` + OpenFlow `set_field:tun_ipv6_dst` gives
  per-flow (per-network!) SIDs through ONE tunnel port. Segment lists
  beyond the first hop remain per-port config. This settles open
  question #1: phase 3 should use one srv6 port per remote chassis (or
  even a single flow-based port), with physical.c setting the SID per
  datapath.

## Days 8–9 close reading (2026-07-14)

### Flow-based tunnels: the mechanism srv6 rides on

OVN main already ships per-flow tunnels (opt-in via `Open_vSwitch
external_ids:ovn-enable-flow-based-tunnels`, checked in
`is_flow_based_tunnels_enabled`, `controller/encaps.c:563`):

- `create_flow_based_tunnels` (`encaps.c:666`): ONE port per encap
  *type* advertised by this chassis, named `ovn<idx>-<type>`, options
  `remote_ip=flow, local_ip=flow, key=flow`
  (`flow_based_tunnel_ensure`, `encaps.c:583`). If `srv6` is a valid
  encap type, the `ovn0-srv6` port comes for free.
- `put_set_tunnel_ip` (`controller/physical.c:234`): per-flow
  `set_field` on `MFF_TUN_IPV6_DST` — exactly the action that forms a
  SID; called from `put_flow_based_encapsulation` (`physical.c:259`).
- `put_flow_based_remote_port_redirect_overlay` (`physical.c:281`):
  the per-remote-port flow builder. Picks the type via
  `select_preferred_tunnel_type`, the destination via
  `select_port_encap_ip(binding, type)`, the ofport via
  `get_flow_based_tunnel_port(type, ctx->flow_tunnels)` (array indexed
  by tunnel type). The srv6 diff concentrates here: compute
  `remote_ip = <remote-locator>:<fn>::` instead of the Encap ip.

### Tunnel type registry

`enum chassis_tunnel_type` (`lib/ovn-util.h:366`): `VXLAN=0, GENEVE=1,
TUNNEL_TYPE_MAX=2`, explicitly "higher number = more preferred"
(`preferred_encap`, `encaps.c:373`). Adding `SRV6=2` makes srv6 win
automatically when both chassis advertise it, and bumping
`TUNNEL_TYPE_MAX` sizes the flow-tunnel array. Name mapping in
`get_tunnel_type` (`lib/ovn-util.c:1023`).

### chassis.c: Encap rows

`chassis_build_encaps` (`controller/chassis.c:681`) creates one SB
`Encap` row per (encap-ip × encap-type) with an options smap (`csum`,
`is_default`). `ovn-encap-type` parsed at `chassis.c:325`. The locator
addition: read `ovn-srv6-locator` (pattern: `get_evpn_vxlan_port`,
`chassis.c:209`) and add `srv6_locator` to the srv6 Encap's options.

### SB schema

`Encap.type` is enum `["geneve", "vxlan"]` (ovn-sb.ovsschema, version
21.10.0; stt already dropped upstream) — add `srv6` + version bump.

### The fn-transport question is solved by an existing knob (option C)

`Logical_Switch other_config:requested-tnl-key` lets NB request the
datapath's SB `tunnel_key` (`northd/en-datapath-logical-switch.c:48`,
honored via `candidate_sdp.requested_tunnel_key` in
`northd/en-datapath-sync.c`). So `SRv6OVNClient` can simply request
`tunnel_key == function ID` — **zero northd changes**, and
`physical.c` forms the SID from `datapath->tunnel_key` it already has.
This supersedes options A and B above. (Fallback/observability: copy
`neutron:srv6-function` via `gather_external_ids`,
`en-datapath-logical-switch.c:110` — the precedented seam that already
copies `name2` and `dynamic-routing-vni`.)
Caveat: requested keys share the space with auto-allocated ones
(routers!) — collision handling is an open question in the design note.

### No VNI on the wire: the RX problem

`put_encapsulation` (`physical.c:151`) loads a 24-bit `MFF_TUN_ID`
(datapath key; geneve additionally carries the port key in a TLV).
**SRv6 has no key field at all** — the destination SID is the only
identifier that survives the wire. So RX must map
`tun_ipv6_dst == <local-locator>:<fn>::` to `MFF_LOG_DATAPATH` and
recover the output port by inner-MAC lookup — exactly OVN's existing
VXLAN "ramp" mode semantics (VNI-only, `is_ramp_switch`). Phase 3
inherits vxlan-mode restrictions initially; encoding the port key in
the SID's *argument* bits (`locator:fn:port`) is the RFC-8986-native
upgrade path later.

Day 10 output: `docs/phase-7.3.2-design.md`.
