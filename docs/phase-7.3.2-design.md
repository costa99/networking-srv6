# Phase 7.3.2 design note — OVN-native SRv6 dataplane

Study exit deliverable (see `phase-7.3.2-study.md` for the process,
`code-map.md` for the per-fact evidence, `cheatsheet.md` for commands).
Everything below is grounded in source read on the OVN main clone
(`/opt/stack/ovn`, commit `3285d816a`) and in the days-6–7 experiments;
nothing is speculative unless marked **open**.

## 0. Summary of the design

One sentence: *register `srv6` as a new OVN encap type carried by the
existing flow-based-tunnel machinery, with the SID formed per flow as
`remote-chassis-locator : datapath-tunnel_key ::`, where Neutron pins
`tunnel_key == function ID` through the existing `requested-tnl-key`
knob.*

The bet from the study plan held: this is a thin adapter, not a
redesign. northd needs **no changes** for the base path. The OVN diff
is: one enum entry, one schema enum value, locator parsing in
chassis.c, and one srv6 branch in physical.c (TX SID formation + RX
SID matching).

## 1. Decisions

### 1.1 SB schema: `Encap.type` gains `srv6`

`ovn-sb.ovsschema`: `"enum": ["set", ["geneve", "vxlan"]]` →
`["geneve", "srv6", "vxlan"]`; version 21.10.0 → 21.11.0; document in
`ovn-sb.xml`. The chassis locator rides in the srv6 `Encap.options`
as `srv6_locator=<prefix>/<len>` (options is an unconstrained string
map — no further schema change). `Encap.ip` stays the underlay
`ovn-encap-ip` (used as the outer IPv6 source).

### 1.2 Chassis locator: operator-set external_id

Each chassis sets (already live on the reference box):

```
ovs-vsctl set Open_vSwitch . \
    external_ids:ovn-encap-type=srv6,geneve \
    external_ids:ovn-encap-ip=<underlay-v6-addr> \
    external_ids:ovn-srv6-locator=fc00:0:1:<node>::/64 \
    external_ids:ovn-enable-flow-based-tunnels=true
```

`controller/chassis.c` parses `ovn-srv6-locator` (same pattern as
`get_evpn_vxlan_port`, `chassis.c:209`) and `chassis_build_encaps`
(`chassis.c:681`) attaches it to the srv6 Encap row's options.
A chassis without a locator must not advertise the srv6 encap
(validation + warning in chassis.c).

### 1.3 Function-ID transport: `requested-tnl-key` (no northd patch)

Neutron's `SRv6OVNClient` sets, on every srv6 `Logical_Switch`:

- `other_config:requested-tnl-key = <function ID>` — honored by
  northd (`en-datapath-logical-switch.c:48`,
  `en-datapath-sync.c` `candidate_sdp.requested_tunnel_key`), making
  SB `Datapath_Binding.tunnel_key == fn`. `physical.c` already has the
  datapath row in hand when building flows — the SID needs no new
  plumbing at all.
- `external_ids:neutron:srv6-function = <fn>` — observability +
  db-sync audit only (copied to SB via `gather_external_ids`,
  `en-datapath-logical-switch.c:110`, if we want it there too).

Key-space note: tunnel_key is 24-bit; `function_bits=16` fits with
room. **Open (1.3a):** requested keys share the allocator with
auto-allocated datapaths (routers). northd resolves collisions in
favor of nobody — a router auto-assigned key N blocks a later network
requesting N. Demo-scale answer: create networks first. Hardening
options (pick in phase 3 review): teach northd to auto-allocate above
`2^function_bits`, or accept and detect via the phase-4 uniqueness
checker (`sb_checks.py` per implementation-plan §6).

### 1.4 Tunnel-port model: flow-based, one port, per-flow SIDs

Decided: **flow-based tunnels only** for srv6 (require
`ovn-enable-flow-based-tunnels=true` on srv6 chassis); no static
per-chassis srv6 ports in phase 3.

- `enum chassis_tunnel_type` (`lib/ovn-util.h:366`): add `SRV6 = 2`,
  `TUNNEL_TYPE_MAX = 3`. Higher = more preferred, so srv6 wins
  whenever both chassis advertise it — the phase-4 "geneve goes
  silent" behavior falls out of the enum order. Map the name in
  `get_tunnel_type` (`lib/ovn-util.c:1023`).
- `create_flow_based_tunnels` (`encaps.c:666`) then creates
  `ovn<idx>-srv6` with `remote_ip=flow, local_ip=flow, key=flow` —
  no encaps.c logic changes beyond letting the type through.
- TX: in `put_flow_based_remote_port_redirect_overlay`
  (`physical.c:281`), srv6 branch: read the remote chassis' srv6
  `Encap.options:srv6_locator`, compute
  `sid = locator_base | (datapath->tunnel_key << (128 - locator_len -
  function_bits))`, pass it as `remote_ip` to
  `put_flow_based_encapsulation` (`physical.c:259`) →
  `put_set_tunnel_ip` loads `MFF_TUN_IPV6_DST` (`physical.c:234`).
  Skip the `MFF_TUN_ID` load for srv6 (no key field on the wire).
  This is *exactly* the mechanism proven by hand in the days-6–7
  experiment (`remote_ip=flow` + per-flow tunnel dst;
  `netdev_srv6_build_header` validates `segs[0] == flow tunnel dst`,
  `netdev-native-tnl.c:970`).
- Hands-on constraint that shapes this: userspace tunnel RX matches on
  the (local, remote) outer pair, so only `flow`-mode ports accept
  packets from arbitrary sources — another reason flow-based is the
  only sane srv6 mode.

### 1.5 RX: the SID is the only wire identifier (VXLAN-ramp semantics)

SRv6 carries no VNI/key. Incoming packets are identified solely by
their destination SID. RX flows (physical.c tunnel-input path):

```
match: in_port=ovn0-srv6, tun_ipv6_dst=<local-locator>:<fn>::
action: set MFF_LOG_DATAPATH=<tunnel_key == fn>, resubmit -> MAC lookup
```

one flow per *local* datapath — the OVS-flow realization of End.DT4/
DT6. The output port is recovered by inner-MAC lookup, i.e. OVN's
existing VXLAN "ramp" mode semantics (`put_encapsulation`'s VNI-only
branch, `physical.c:168`, `is_ramp_switch`). Phase 3 therefore
inherits the documented vxlan-mode feature restrictions for srv6
networks (acceptable: the demo is L2 E-W traffic).

**Upgrade path (not phase 3):** encode the port key in the SID's
RFC 8986 *argument* bits — `locator : fn : port-key ::` — restoring
full geneve-equivalent semantics. Room exists: /64 locator + 16 fn +
16 port leaves 32 zero bits.

**Open (1.5a):** verify the flow-based srv6 port delivers
`MFF_TUN_IPV6_DST` on RX and that `netdev_srv6_pop_header`
(`netdev-native-tnl.c:1047`) accepts SRH-less reduced encap from
kernel/FRR peers. First experiment of phase 3.

### 1.6 Datapath: `br-int` goes netdev on srv6 chassis

Experimentally confirmed: OVS srv6 vports exist only in the userspace
datapath (`netdev_srv6_{build,push,pop}_header` in
`netdev-native-tnl.c`; kernel has no `srv6` link kind). Options
weighed:

- (a) **`br-int` with `datapath_type=netdev` — chosen.** One
  `ovs-vsctl set bridge br-int datapath_type=netdev` per srv6 chassis
  (devstack plugin does it). Cost: userspace forwarding for all
  traffic on that chassis; fine for a thesis demo, measured and
  reported honestly.
- (b) separate netdev bridge patched to a system br-int — rejected:
  OVS patch ports cannot cross datapath types; a veth splice would
  reintroduce the kernel path and its srv6 ignorance anyway.
- (c) kernel `seg6`/`seg6local` agent outside OVS — remains the
  descope-gate fallback (implementation-plan §5), not the design.

#### 1.7 MTU and flow label

**Correction from the wire capture:** OVS does *not* do reduced encap —
`netdev_srv6_build_header` unconditionally writes an SRH
(`IPPROTO_ROUTING`, `hdrlen = 2 × nsegs`), so even a single-SID packet
carries `40 (IPv6) + 8 (SRH base) + 16 (one segment) = 64 bytes` of
overhead (verified: every captured OVS packet in the days-6–7 pcap has
`RT6 len=2` at one segment). The phase-7.3.1b `get_mtu()` subtracts
only 40 — **follow-up for phase 3:** change the constant to
`IPV6_HEADER_LEN + SRH_BASE_LEN + SRH_SEGMENT_LEN` (all already in
`networking_srv6/constants.py`) = 64.

Set `srv6_flowlabel=compute` on the flow-based port for ECMP entropy
in the underlay (option parsed at `netdev-vport.c:799`).

## 2. Phase-3 patch series (target shape)

0. **OVS: `parse_srv6_segs` strtok fix** — done, exported as
   `docs/ovn-patches/0001-…`, submit to ovs-dev independently (bug
   affects any reconfigure with multi-segment lists).
1. `lib/ovn-util.[ch]`: `SRV6=2` in `chassis_tunnel_type`,
   `TUNNEL_TYPE_MAX=3`, `get_tunnel_type("srv6")`.
2. `ovn-sb.ovsschema` + `ovn-sb.xml`: Encap enum + `srv6_locator`
   option docs; version bump.
3. `controller/chassis.c`: accept `srv6` in `ovn-encap-type`; parse
   `ovn-srv6-locator`; attach to Encap options; refuse srv6 without a
   locator.
4. `controller/physical.c`: TX srv6 branch in
   `put_flow_based_remote_port_redirect_overlay` (SID formation);
   RX per-local-datapath SID match flows; skip TUN_ID for srv6.
5. `controller/encaps.c`: nothing beyond type plumbing expected
   (flow-based path is generic); BFD interaction check
   (`bfd.c:217` consumes flow-based state).
6. OVN system test: two sandbox/fake chassis, srv6 encap, port
   binding each side, ping + SRH assertion (reuse the days-6–7 test
   patterns).

Neutron side (this repo, unchanged from implementation-plan §5.2/5.3):
`SRv6OVNClient` writes `requested-tnl-key` + `neutron:srv6-function`;
subclassed mech driver replaces the `ovn_compat.py` shim; devstack
plugin sets the four external_ids from §1.2 and flips br-int to
netdev.

## 3. Open questions (carried into phase 3)

1. (1.5a) Flow-based srv6 RX: does `MFF_TUN_IPV6_DST` reach table 0,
   and does pop accept reduced encap from non-OVS peers? — first
   phase-3 experiment, in the OVN sandbox.
2. (1.3a) `requested-tnl-key` collisions with auto-allocated router
   keys — decide demo-accept vs northd allocation-floor patch.
3. BFD over flow-based srv6 tunnels (`bfd.c:217` warns it's
   flow-based-aware; untested for a new type).
4. Live migration + flow-based tunnels: flows are rebuilt from
   Port_Binding chassis moves; expected to just work, verify in
   phase 4 (implementation-plan §6 test 6).
5. Multi-segment TE (waypoint SIDs) through OVN: per-flow segment
   lists are NOT supported by OVS (`srv6_segs` is per-port config) —
   the phase-6b TE demo uses kernel encap or dedicated ports; a
   per-flow segs OVS extension is thesis-future-work.
