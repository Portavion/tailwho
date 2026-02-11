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

## Common options

- `--dry-run`: list matching nodes without switching exit nodes
- `--filter <text>`: filter by hostname, country, or city
- `--limit <n>`: benchmark only first `n` matches
- `--include-offline`: include nodes marked offline
- `--ping-host <host>`: ping target (default `google.com`)
- `--ping-count <n>`: ping probes per node (default `3`)
- `--download-url <url>`: URL for throughput test
- `--download-bytes <n>`: max bytes to read for throughput estimate
- `--download-timeout <s>`: download timeout seconds
- `--switch-timeout <s>`: wait time for exit-node switch to activate
- `--json-out <file>`: write JSON results
- `--csv-out <file>`: write CSV results
- `--no-restore`: do not restore original exit-node state after run

## How it behaves

- Discovers Mullvad exit nodes from `tailscale status --json`
- Sorts nodes by country/city/hostname
- Benchmarks each node sequentially
- Prints a summary table sorted by status and performance
- Restores your original exit-node state in a `finally` block (unless `--no-restore`)

## Troubleshooting

- `failed to connect to local Tailscaled process`:
  - Ensure Tailscale is running and authenticated: `tailscale status`
- `cannot resolve google.com: Unknown host`:
  - DNS is failing on the active route; try a different node or increase switch timeout
- Frequent `switch-timeout`:
  - Increase `--switch-timeout` (for example `--switch-timeout 40`)
- Slow tests:
  - Lower `--download-bytes` (for example `--download-bytes 1000000`)

## Notes

- This tool changes your active exit node while it runs.
- Running against all available Mullvad nodes can take significant time.
