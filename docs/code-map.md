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

## Remaining for phase 2 (study days 6–10)

- Days 6–7 hands-on: two-netns srv6 tunnel-port traffic on netdev
  bridges; record which option shapes work per-flow; tcpdump the SRH.
- Days 8–9: read `physical.c` output path + northd `join_datapaths`
  external_ids copying (for option B above).
- Day 10: write `docs/phase-7.3.2-design.md` deciding: Encap schema
  addition, locator external_id name, fn source (option A vs B),
  datapath strategy (a/b/c above).
