# tailwho

CLI benchmark tool for Tailscale Mullvad exit nodes.

It iterates over available Mullvad exit nodes, switches to each node, checks connectivity with a ping to Google, measures latency, and estimates download speed.

## Requirements

- macOS/Linux with `tailscale` CLI installed and authenticated
- Access to Mullvad exit nodes in your Tailscale account
- Python 3.9+ (no third-party Python dependencies)
- Permission to run `tailscale set --exit-node ...`

## Install as `tailwho`

### User-local install (recommended)

```bash
mkdir -p "$HOME/.local/bin"
ln -sf "/Users/portavion/code/tailwho/mullvad_exitnode_benchmark.py" "$HOME/.local/bin/tailwho"
grep -q 'HOME/.local/bin' "$HOME/.zshrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
source "$HOME/.zshrc"
```

### Global install

```bash
sudo ln -sf "/Users/portavion/code/tailwho/mullvad_exitnode_benchmark.py" /usr/local/bin/tailwho
```

## Quick start

List candidate nodes only:

```bash
tailwho --dry-run --limit 10
```

Run a full benchmark:

```bash
tailwho
```

Filter and test a subset:

```bash
tailwho --filter us- --limit 20
```

Write machine-readable output:

```bash
tailwho --json-out results.json --csv-out results.csv
```

Deep benchmark only survivors from a previous fast run:

```bash
tailwho --ok-from-json quick.json --ping-count 3 --ping-timeout 2 --download-url "https://speed.cloudflare.com/__down?bytes=5000000" --download-bytes 5000000 --download-timeout 30 --switch-timeout 12 --json-out deep.json --csv-out deep.csv
```

## Common options

- `--dry-run`: list matching nodes without switching exit nodes
- `--filter <text>`: filter by hostname, country, or city
- `--limit <n>`: benchmark only first `n` matches
- `--include-offline`: include nodes marked offline
- `--target <hostname-or-text>`: repeatable; extracts any `*.mullvad.ts.net` hostname from the value
- `--targets-file <path>`: file with hostnames or pasted table output; extracts all `*.mullvad.ts.net` hostnames
- `--ok-from-json <file>`: previous `--json-out` file; selects rows where `status=ok`
- `--ping-host <host>`: ping target (default `8.8.8.8`)
- `--ping-count <n>`: ping probes per node (default `1`)
- `--ping-timeout <s>`: per-ping timeout (default `1.0`)
- `--download-url <url>`: URL for throughput test (default Cloudflare `300000`-byte endpoint)
- `--download-bytes <n>`: max bytes to read for throughput estimate (default `300000`)
- `--download-timeout <s>`: download timeout seconds (default `6.0`)
- `--switch-timeout <s>`: wait time for exit-node switch to activate (default `8.0`)
- `--json-out <file>`: write JSON results
- `--csv-out <file>`: write CSV results
- `--no-restore`: do not restore original exit-node state after run

## How it behaves

- Discovers Mullvad exit nodes from `tailscale status --json`
- Sorts nodes by country/city/hostname by default
- If `--targets-file` / `--target` / `--ok-from-json` are used, benchmarks only requested hostnames (in requested order)
- Benchmarks each node sequentially
- Prints a summary table sorted by status and performance
- Restores your original exit-node state in a `finally` block (unless `--no-restore`)

## Troubleshooting

- `failed to connect to local Tailscaled process`:
  - Ensure Tailscale is running and authenticated: `tailscale status`
- `cannot resolve host`:
  - DNS is failing on the active route; use `--ping-host 8.8.8.8` or try a different node
- Frequent `switch-timeout`:
  - Increase `--switch-timeout` (for example `--switch-timeout 40`)
- Slow tests:
  - Lower `--download-bytes` (for example `--download-bytes 1000000`)

## Notes

- This tool changes your active exit node while it runs.
- Running against all available Mullvad nodes can take significant time.
