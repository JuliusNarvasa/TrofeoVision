# Air Quality tile backgrounds

Drop custom PNG backgrounds here as `<aqi_state>_<bucket>.png`:

| State | OWM AQI |
|---|---|
| `good` | 1 |
| `fair` | 2 |
| `moderate` | 3 |
| `poor` | 4 |
| `very_poor` | 5 |

| Bucket | Local time |
|---|---|
| `morning` | 05:00 – 11:59 |
| `noon` | 12:00 – 12:59 |
| `afternoon` | 13:00 – 17:59 |
| `evening` | 18:00 – 21:59 |
| `night` | 22:00 – 04:59 |

Maximum 25 files = 5 states × 5 buckets. All PNGs must be **462 × 280 px**
to match the greeting tile exactly (the same size the weather backgrounds
use).

## Fallback behavior

When a `(state, bucket)` combo has no matching PNG, the renderer falls
back to `good_morning.png`, then to a category-color vertical gradient
generated in memory. The fallback is intentional — the air-quality tile
works out of the box with zero custom assets.

## Recommended style

Soft, atmospheric photography that suggests air quality:

- `good`      — clear sky, mountains, clean cityscape
- `fair`      — light haze, distant clouds
- `moderate`  — overcast city, mild fog
- `poor`      — smog, dense haze, urban skyline
- `very_poor` — heavy smog, orange-tinged sky, masked pedestrians

A top→bottom dark readability overlay is applied at render time (same
overlay as the weather tile), so the PNG only needs to be "vibes" —
text legibility is handled by the renderer.
