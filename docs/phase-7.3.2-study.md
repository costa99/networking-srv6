# Phase 7.3.2 study plan — OVN-native SRv6 dataplane

Two-week timebox (hard stop). The exit criterion is the **design note**
written, not a feeling of understanding. If open questions remain at
the deadline, they go in the note as open questions.

## Decisions already made (do not re-litigate during the study)

- **Architecture: OVN-native.** OVN itself is patched; no sidecar agent.
- **Minimal-adapter scope.** OVN today has no `srv6` encap (only
  geneve/vxlan/stt); OVS >= 3.2 already implements SRv6 tunnel ports
  (the chassis advertises `srv6` in `iface-types`). The OVN patch is
  therefore a thin adapter: teach SB/ovn-controller to *configure* the
  existing OVS feature. No logical-flow/northd redesign in 7.3.2.
- **Staged environments.** Develop the OVN patch against OVN's own
  build/sandbox/system-test tooling. Devstack multi-node (with IPv6
  underlay) is demo infrastructure, added later.
- **Open item (parked):** source a second machine/VM for the eventual
  two-chassis demo.

## Deliverables (all four, end of week 2)

1. **Design note** `docs/phase-7.3.2-design.md`: exact SB schema
   additions, `encaps.c` / `physical.c` / `chassis.c` touch points,
   how a chassis learns/publishes its locator, and how the per-network
   locator (from `SRv6TypeDriver.get_locator()`) reaches OVN.
2. **Code map** `docs/code-map.md`: the traced path
   `openstack network create` -> ML2 -> OVN mech driver -> `ovn_client`
   -> NB -> northd -> SB -> `ovn-controller` -> OVS flows/tunnel ports,
   with `file:function` at every hop.
3. **Command cheatsheet** `docs/cheatsheet.md` (see starter below).
4. **Warm-up milestone (done = observed):** OVN cloned and built from
   source, `make sandbox` running, one trivial patch (e.g. a
   `VLOG_INFO` in `controller/encaps.c`) compiled and its log line seen.

## Week 1 — build, trace, map

- **Days 1–2 — warm-up build.** Clone `ovn` (+ its `ovs` submodule),
  `./boot.sh && ./configure && make`, run `make sandbox`. Apply the
  trivial log-line patch, rebuild, observe. Prefer building on the
  devstack box (same OS as the demo); local build is an acceptable
  fallback for iteration speed.
- **Days 2–3 — trace the live system.** Read `ovn-architecture(7)`
  (the single most important document), then on the devstack box create
  an srv6 network/port and follow it with the cheatsheet commands
  through NB -> SB -> br-int flows. Write the code map as you go.
- **Days 4–5 — neutron side of the map.** Read
  `neutron/plugins/ml2/drivers/ovn/mech_driver/ovsdb/ovn_client.py`
  (`create_network`, `_gen_network_parameters`) and note exactly where
  `network_type`/`segmentation_id` land in NB (`Logical_Switch`
  `external_ids`/`other_config`). Identify where the locator would ride.

## Week 2 — prove the primitive, spec the patch

- **Days 6–7 — OVS srv6 tunnel port hands-on, no OVN.** On the box (or
  two netns), create an `srv6`-type tunnel port with `ovs-vsctl` by
  hand, pass traffic, capture with tcpdump and confirm the SRH on the
  wire. Record the exact option names the port takes. This proves the
  primitive the whole architecture leans on — if it does not work, the
  design note must say so and reconsider.
- **Days 8–9 — the C touch points.** Read with one question in mind
  ("what is the minimal diff for a new encap type?"):
  - `controller/chassis.c` — how `ovn-encap-type`/`ovn-encap-ip`
    external_ids become SB `Encap` rows;
  - `controller/encaps.c` — how SB `Encap` rows become OVS tunnel
    ports;
  - `controller/physical.c` — how output flows pick a tunnel and set
    metadata;
  - `northd/` — skim only for datapath `tunnel_key` handling.
  Optionally reproduce a two-chassis topology with ovn-fake-multinode.
- **Day 10 — write the design note.** Including: SB `Encap` type
  addition; where the chassis locator lives (`ovn-cms-options` vs new
  column); how networking-srv6 injects the per-network locator
  (probable answer: a small neutron-side extension writing it into
  `Logical_Switch` external_ids); list of open questions.

## Bounded reading list (this is the whole curriculum)

**Docs:** `ovn-architecture(7)`, `ovn-nb(5)`, `ovn-sb(5)`,
`ovn-controller(8)`; OVS docs for the srv6 tunnel port type;
`ip-route(8)` seg6/seg6local (kernel `End.DT4`/`End.DT6` semantics).

**Protocol theory (thesis background):** RFC 8754 (SRH),
RFC 8986 (SRv6 network programming — defines End.DT4/End.DT6).

**Source:** the three `controller/*.c` files above; `ovn_client.py` on
the neutron side. Everything else is lookup, not curriculum.

## Command cheatsheet starter

```bash
# NB: what neutron asked for
ovn-nbctl show
ovn-nbctl list Logical_Switch

# SB: what northd derived, who the chassis are, how they tunnel
ovn-sbctl show
ovn-sbctl list Chassis
ovn-sbctl list Encap
ovn-sbctl list Port_Binding

# simulate a packet through logical flows
ovn-trace <datapath> '<microflow>'

# what actually hit the switch
ovs-vsctl show
ovs-vsctl list interface <port>          # tunnel options live here
ovs-ofctl dump-flows br-int
ovs-appctl ofproto/trace br-int '<flow>'

# kernel SRv6 reference semantics
ip -6 route add <prefix> encap seg6 mode encap segs <sid[,sid]> dev <if>
ip -6 route add <sid> encap seg6local action End.DT4 vrftable <t> dev <if>
```
