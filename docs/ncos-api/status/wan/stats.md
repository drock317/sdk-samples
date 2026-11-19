# status/wan/stats

<!-- path: status/wan/stats -->
<!-- type: status -->
<!-- response: object -->

[status](../) / [wan](.) / stats

---

Aggregate traffic statistics for the active WAN connection.

### Response Type
`object`

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `in` | integer | Total bytes received |
| `out` | integer | Total bytes sent |
| `ipackets` | integer | Packets received |
| `opackets` | integer | Packets sent |
| `ierrors` | integer | Receive errors |
| `oerrors` | integer | Transmit errors |
| `idrops` | integer | Received packets dropped |
| `bps` | integer | Bits per second, ingress + egress (`ibps` + `obps`) |
| `ibps` | integer | Ingress **bits**/sec |
| `obps` | integer | Egress **bits**/sec |
| `collisions` | integer | Collision count |
| `imcasts` | integer | Incoming multicast |
| `omcasts` | integer | Outgoing multicast |
| `noproto` | integer | Unknown protocol |
| `ts` | float | Device clock in seconds with sub-second precision (advances in real time; behaves like uptime) |

> Units verified on an R1900 (2026-11-19): `8 x delta(in) / delta(ts)` equals the reported
> `ibps` (and likewise `out`/`obps`), so `in`/`out` are bytes and `ibps`/`obps` are bits per
> second, not bytes per second. This object sums **every** WAN device, including `sdwan` and
> `wbond` overlays, so it double counts traffic. See `docs/analytics/counter-semantics.md`.

### SDK Example
```python
import cp
stats = cp.get('status/wan/stats')
if stats and isinstance(stats, dict):
    in_mb = stats.get('in', 0) / (1024 * 1024)
    out_mb = stats.get('out', 0) / (1024 * 1024)
    cp.log(f'WAN traffic: {in_mb:.1f} MB in, {out_mb:.1f} MB out')
```

### REST
```
GET /api/status/wan/stats
```

### Example Response
```json
{
  "success": true,
  "data": {
    "bps": 0,
    "collisions": 0,
    "ibps": 0,
    "idrops": 0,
    "ierrors": 0,
    "imcasts": 0,
    "in": 4223948,
    "ipackets": 18133,
    "noproto": 0,
    "obps": 0,
    "oerrors": 0,
    "omcasts": 0,
    "opackets": 19840,
    "out": 6286105,
    "ts": 157861.749640768
  }
}
```

### Related
- [devices/{id}/stats](devices/stats.md) - Per-device stats
