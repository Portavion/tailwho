#!/usr/bin/env python3
"""
Benchmark Mullvad exit nodes available in Tailscale.

For each Mullvad exit node:
1) Switch to that node as the active exit node.
2) Ping Google to confirm connectivity and collect latency.
3) Run a small download test to estimate throughput.

Usage examples:
  ./mullvad_exitnode_benchmark.py
  ./mullvad_exitnode_benchmark.py --limit 10
  ./mullvad_exitnode_benchmark.py --filter us- --download-bytes 10000000
  ./mullvad_exitnode_benchmark.py --json-out results.json --csv-out results.csv
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


PING_AVG_RE = re.compile(
    r"(?:round-trip|rtt)\s+min/avg/max/(?:stddev|mdev)\s*=\s*[\d.]+/([\d.]+)/"
)
PING_LOSS_RE = re.compile(r"([\d.]+)%\s+packet loss")
HOSTNAME_RE = re.compile(r"([a-z0-9][a-z0-9.-]*\.mullvad\.ts\.net\.?)", re.IGNORECASE)


@dataclass
class ExitNode:
    node_id: str
    hostname: str
    country: str
    city: str
    online: bool
    tailscale_ip: Optional[str]


def run_command(
    args: List[str],
    *,
    check: bool = True,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        details = stderr or stdout or "unknown error"
        raise RuntimeError(f"command failed ({' '.join(args)}): {details}")
    return proc


def tailscale_status(include_peers: bool) -> Dict[str, Any]:
    cmd = ["tailscale", "status", "--json"]
    if not include_peers:
        cmd.append("--peers=false")
    proc = run_command(cmd, check=True)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        sample = (proc.stdout or "")[:200]
        raise RuntimeError(f"failed to parse tailscale status JSON: {exc}; sample={sample!r}") from exc


def normalize_dns(name: Optional[str]) -> str:
    return (name or "").rstrip(".")


def normalize_hostname(name: str) -> str:
    return name.strip().rstrip(".").lower()


def extract_hostnames(text: str) -> List[str]:
    hosts: List[str] = []
    seen = set()
    for match in HOSTNAME_RE.findall(text):
        host = normalize_hostname(match)
        if host not in seen:
            hosts.append(host)
            seen.add(host)
    return hosts


def load_ok_hostnames_from_json(path: str) -> List[str]:
    json_path = Path(path).expanduser()
    raw = json_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise RuntimeError(f"--ok-from-json expects a list of rows, got {type(payload).__name__}")

    hosts: List[str] = []
    seen = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("status", "")) != "ok":
            continue
        hostname = item.get("hostname")
        if not isinstance(hostname, str):
            continue
        host = normalize_hostname(hostname)
        if host and host not in seen:
            hosts.append(host)
            seen.add(host)
    return hosts


def load_target_hostnames(
    inline_targets: List[str],
    targets_file: str,
    ok_from_json: str,
) -> List[str]:
    hosts: List[str] = []
    seen = set()

    def add_host(host: str) -> None:
        normalized = normalize_hostname(host)
        if normalized and normalized not in seen:
            hosts.append(normalized)
            seen.add(normalized)

    if ok_from_json:
        for host in load_ok_hostnames_from_json(ok_from_json):
            add_host(host)

    if targets_file:
        if targets_file == "-":
            text = sys.stdin.read()
        else:
            target_path = Path(targets_file).expanduser()
            text = target_path.read_text(encoding="utf-8")
        for host in extract_hostnames(text):
            add_host(host)

    for raw in inline_targets:
        extracted = extract_hostnames(raw)
        if extracted:
            for host in extracted:
                add_host(host)
        else:
            # Allow direct hostname input without surrounding table text.
            add_host(raw)

    return hosts


def select_nodes_by_hostnames(
    nodes: List[ExitNode],
    target_hostnames: List[str],
) -> Tuple[List[ExitNode], List[str]]:
    by_hostname = {normalize_hostname(n.hostname): n for n in nodes}
    selected: List[ExitNode] = []
    missing: List[str] = []
    for hostname in target_hostnames:
        node = by_hostname.get(normalize_hostname(hostname))
        if node is None:
            missing.append(hostname)
        else:
            selected.append(node)
    return selected, missing


def first_ip(peer: Dict[str, Any]) -> Optional[str]:
    ips = peer.get("TailscaleIPs") or []
    if not ips:
        return None
    first = ips[0]
    return first.split("/", 1)[0]


def discover_mullvad_exit_nodes(status: Dict[str, Any]) -> List[ExitNode]:
    peers = status.get("Peer") or {}
    nodes: List[ExitNode] = []
    for peer in peers.values():
        if not peer.get("ExitNodeOption"):
            continue

        tags = peer.get("Tags") or []
        dns_name = normalize_dns(peer.get("DNSName"))
        is_mullvad = "tag:mullvad-exit-node" in tags or ".mullvad.ts.net" in dns_name
        if not is_mullvad:
            continue

        node_id = peer.get("ID")
        if not node_id:
            continue

        hostname = dns_name or peer.get("HostName") or node_id
        location = peer.get("Location") or {}
        country = location.get("Country") or ""
        city = location.get("City") or ""
        node = ExitNode(
            node_id=node_id,
            hostname=hostname,
            country=country,
            city=city,
            online=bool(peer.get("Online", False)),
            tailscale_ip=first_ip(peer),
        )
        nodes.append(node)

    nodes.sort(key=lambda n: (n.country, n.city, n.hostname))
    return nodes


def build_peer_target_by_id(status: Dict[str, Any]) -> Dict[str, str]:
    peers = status.get("Peer") or {}
    out: Dict[str, str] = {}
    for peer in peers.values():
        node_id = peer.get("ID")
        if not node_id:
            continue
        dns_name = normalize_dns(peer.get("DNSName"))
        host = peer.get("HostName") or ""
        ip = first_ip(peer) or ""
        target = dns_name or host or ip
        if target:
            out[node_id] = target
    return out


def get_current_exit_target(status: Dict[str, Any], id_to_target: Dict[str, str]) -> Optional[str]:
    exit_status = status.get("ExitNodeStatus") or {}
    node_id = exit_status.get("ID")
    if node_id and node_id in id_to_target:
        return id_to_target[node_id]

    ip_list = exit_status.get("TailscaleIPs") or []
    if ip_list:
        return str(ip_list[0]).split("/", 1)[0]
    return None


def set_exit_node(target: str) -> None:
    run_command(["tailscale", "set", "--exit-node", target], check=True)


def clear_exit_node() -> None:
    run_command(["tailscale", "set", "--exit-node="], check=True)


def wait_for_active_exit_node(node_id: str, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = tailscale_status(include_peers=False)
        exit_status = status.get("ExitNodeStatus") or {}
        if exit_status.get("ID") == node_id:
            return True
        time.sleep(1.0)
    return False


def ping_google(host: str, count: int, per_probe_timeout_s: float) -> Dict[str, Optional[float]]:
    if platform.system().lower() == "darwin":
        timeout_flag = str(max(1, int(per_probe_timeout_s * 1000)))
    else:
        timeout_flag = str(max(1, int(math.ceil(per_probe_timeout_s))))

    cmd = ["ping", "-n", "-c", str(count), "-W", timeout_flag, host]
    total_timeout = max(20.0, count * max(per_probe_timeout_s, 1.0) + 15.0)
    proc = run_command(cmd, check=False, timeout=total_timeout)
    combined = f"{proc.stdout}\n{proc.stderr}"

    avg_ms = None
    packet_loss = None
    avg_match = PING_AVG_RE.search(combined)
    if avg_match:
        avg_ms = float(avg_match.group(1))

    loss_match = PING_LOSS_RE.search(combined)
    if loss_match:
        packet_loss = float(loss_match.group(1))

    success = proc.returncode == 0 and avg_ms is not None
    return {
        "success": 1.0 if success else 0.0,
        "avg_ms": avg_ms,
        "packet_loss_pct": packet_loss,
        "raw_output": combined.strip(),
    }


def download_speed_mbps(
    url: str,
    timeout_s: float,
    max_bytes: int,
) -> Dict[str, Optional[float]]:
    req = Request(
        url,
        headers={
            "User-Agent": "mullvad-exitnode-benchmark/1.0",
            "Accept-Encoding": "identity",
        },
    )
    start = time.perf_counter()
    total = 0
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes > 0 and total >= max_bytes:
                    break
    except (
        URLError,
        HTTPError,
        TimeoutError,
        OSError,
        http.client.IncompleteRead,
    ) as exc:
        return {
            "success": 0.0,
            "mbps": None,
            "bytes": float(total),
            "seconds": None,
            "error": str(exc),
        }

    elapsed = time.perf_counter() - start
    if elapsed <= 0 or total <= 0:
        return {
            "success": 0.0,
            "mbps": None,
            "bytes": float(total),
            "seconds": elapsed,
            "error": "no data downloaded",
        }

    mbps = (total * 8) / elapsed / 1_000_000
    return {
        "success": 1.0,
        "mbps": mbps,
        "bytes": float(total),
        "seconds": elapsed,
        "error": None,
    }


def format_num(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def status_rank(status: str) -> int:
    order = {
        "ok": 0,
        "download-failed": 1,
        "ping-failed": 2,
        "switch-timeout": 3,
        "set-failed": 4,
    }
    return order.get(status, 99)


def print_summary(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("No benchmark results.")
        return

    rows_sorted = sorted(
        rows,
        key=lambda r: (
            status_rank(str(r.get("status", ""))),
            -float(r.get("download_mbps") or -1),
            float(r.get("latency_ms") or 1_000_000),
            str(r.get("hostname", "")),
        ),
    )

    headers = ["hostname", "country", "city", "latency_ms", "download_mbps", "status"]
    table_rows: List[List[str]] = []
    for r in rows_sorted:
        table_rows.append(
            [
                str(r.get("hostname", "")),
                str(r.get("country", "")),
                str(r.get("city", "")),
                format_num(r.get("latency_ms")),
                format_num(r.get("download_mbps")),
                str(r.get("status", "")),
            ]
        )

    widths = [len(h) for h in headers]
    for row in table_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = "  ".join("-" * widths[i] for i in range(len(headers)))
    print()
    print(header_line)
    print(sep_line)
    for row in table_rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))

    ok_count = sum(1 for r in rows if r.get("status") == "ok")
    print()
    print(f"Successful nodes: {ok_count}/{len(rows)}")


def write_json(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = [
        "hostname",
        "country",
        "city",
        "node_id",
        "online",
        "status",
        "latency_ms",
        "packet_loss_pct",
        "download_mbps",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fieldnames})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Mullvad exit nodes on Tailscale."
    )
    parser.add_argument("--ping-host", default="8.8.8.8", help="Host/IP to ping (default: 8.8.8.8)")
    parser.add_argument("--ping-count", type=int, default=1, help="ICMP probe count per node (default: 1)")
    parser.add_argument(
        "--ping-timeout",
        type=float,
        default=1.0,
        help="Per-ping timeout in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--download-url",
        default="https://speed.cloudflare.com/__down?bytes=300000",
        help="Download URL used for throughput test",
    )
    parser.add_argument(
        "--download-bytes",
        type=int,
        default=300_000,
        help="Maximum bytes to read from download URL (default: 300000)",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=6.0,
        help="Download timeout in seconds (default: 6.0)",
    )
    parser.add_argument(
        "--switch-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for exit node switch (default: 8.0)",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Benchmark a specific hostname (repeatable); accepts table text and extracts mullvad hostnames",
    )
    parser.add_argument(
        "--targets-file",
        default="",
        help="File containing hostnames/table text; extracts all *.mullvad.ts.net hostnames. Use '-' for stdin",
    )
    parser.add_argument(
        "--ok-from-json",
        default="",
        help="Previous --json-out file; benchmarks rows where status=ok",
    )
    parser.add_argument("--filter", default="", help="Substring filter for hostname/country/city")
    parser.add_argument("--limit", type=int, default=0, help="Only test first N matching nodes")
    parser.add_argument("--include-offline", action="store_true", help="Include offline nodes")
    parser.add_argument("--dry-run", action="store_true", help="Only list matching nodes and exit")
    parser.add_argument("--json-out", default="", help="Optional JSON output file")
    parser.add_argument("--csv-out", default="", help="Optional CSV output file")
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="Do not restore original exit-node state at the end",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        initial_status = tailscale_status(include_peers=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to query Tailscale status: {exc}", file=sys.stderr)
        return 2

    id_to_target = build_peer_target_by_id(initial_status)
    original_exit_target = get_current_exit_target(initial_status, id_to_target)
    nodes = discover_mullvad_exit_nodes(initial_status)
    missing_targets: List[str] = []
    target_hostnames: List[str] = []

    if not args.include_offline:
        nodes = [n for n in nodes if n.online]

    try:
        target_hostnames = load_target_hostnames(args.target, args.targets_file, args.ok_from_json)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load target hostnames: {exc}", file=sys.stderr)
        return 2

    if target_hostnames:
        nodes, missing_targets = select_nodes_by_hostnames(nodes, target_hostnames)

    if args.filter:
        needle = args.filter.lower()
        nodes = [
            n
            for n in nodes
            if needle in n.hostname.lower()
            or needle in n.country.lower()
            or needle in n.city.lower()
        ]

    if args.limit and args.limit > 0:
        nodes = nodes[: args.limit]

    if not nodes:
        print("No Mullvad exit nodes matched the filters.")
        return 1

    print(f"Matched {len(nodes)} Mullvad exit nodes.")
    if target_hostnames:
        print(f"Target hostnames requested: {len(target_hostnames)}")
    if missing_targets:
        preview = ", ".join(missing_targets[:5])
        suffix = " ..." if len(missing_targets) > 5 else ""
        print(f"Warning: {len(missing_targets)} requested hostnames were not found: {preview}{suffix}")
    if args.dry_run:
        for n in nodes:
            print(f"- {n.hostname} ({n.country}, {n.city})")
        return 0

    results: List[Dict[str, Any]] = []
    try:
        for idx, node in enumerate(nodes, start=1):
            print(f"[{idx}/{len(nodes)}] Testing {node.hostname} ({node.country}, {node.city}) ...")
            row: Dict[str, Any] = {
                "hostname": node.hostname,
                "country": node.country,
                "city": node.city,
                "node_id": node.node_id,
                "online": node.online,
                "status": "set-failed",
                "latency_ms": None,
                "packet_loss_pct": None,
                "download_mbps": None,
                "error": "",
            }

            try:
                set_exit_node(node.hostname)
            except Exception as exc:  # noqa: BLE001
                row["status"] = "set-failed"
                row["error"] = str(exc)
                results.append(row)
                print(f"  set failed: {exc}")
                continue

            try:
                active = wait_for_active_exit_node(node.node_id, args.switch_timeout)
            except Exception as exc:  # noqa: BLE001
                row["status"] = "switch-timeout"
                row["error"] = f"failed while waiting for switch: {exc}"
                results.append(row)
                print(f"  switch check failed: {exc}")
                continue

            if not active:
                row["status"] = "switch-timeout"
                row["error"] = f"timed out waiting for node ID {node.node_id}"
                results.append(row)
                print("  switch timeout")
                continue

            try:
                ping = ping_google(args.ping_host, args.ping_count, args.ping_timeout)
            except Exception as exc:  # noqa: BLE001
                row["status"] = "ping-failed"
                row["error"] = f"ping command failed: {exc}"
                results.append(row)
                print(f"  ping command failed: {exc}")
                continue

            row["latency_ms"] = ping.get("avg_ms")
            row["packet_loss_pct"] = ping.get("packet_loss_pct")
            ping_ok = bool(ping.get("success", 0.0))
            if not ping_ok:
                row["status"] = "ping-failed"
                raw_ping = str(ping.get("raw_output") or "").strip()
                row["error"] = raw_ping.splitlines()[-1] if raw_ping else "ping failed"
                results.append(row)
                print(
                    "  ping failed"
                    f" (avg={format_num(row['latency_ms'])}ms, loss={format_num(row['packet_loss_pct'])}%)"
                )
                continue

            try:
                dl = download_speed_mbps(
                    args.download_url,
                    args.download_timeout,
                    args.download_bytes,
                )
            except Exception as exc:  # noqa: BLE001
                row["status"] = "download-failed"
                row["error"] = f"download probe failed: {exc}"
                results.append(row)
                print(
                    "  download failed"
                    f" (avg={format_num(row['latency_ms'])}ms): {row['error']}"
                )
                continue
            if not bool(dl.get("success", 0.0)):
                row["status"] = "download-failed"
                row["error"] = str(dl.get("error") or "download failed")
                results.append(row)
                print(
                    "  download failed"
                    f" (avg={format_num(row['latency_ms'])}ms): {row['error']}"
                )
                continue

            row["download_mbps"] = dl.get("mbps")
            row["status"] = "ok"
            results.append(row)
            print(
                "  ok"
                f" avg={format_num(row['latency_ms'])}ms"
                f" down={format_num(row['download_mbps'])}Mbps"
            )
    finally:
        if args.no_restore:
            print("Skipped restoring original exit-node state (--no-restore).")
        else:
            try:
                if original_exit_target:
                    print(f"Restoring original exit node: {original_exit_target}")
                    set_exit_node(original_exit_target)
                else:
                    print("Clearing exit node (none was originally set).")
                    clear_exit_node()
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: failed to restore original exit-node state: {exc}", file=sys.stderr)

    print_summary(results)

    if args.json_out:
        json_path = Path(args.json_out).expanduser().resolve()
        write_json(json_path, results)
        print(f"Wrote JSON: {json_path}")
    if args.csv_out:
        csv_path = Path(args.csv_out).expanduser().resolve()
        write_csv(csv_path, results)
        print(f"Wrote CSV: {csv_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
