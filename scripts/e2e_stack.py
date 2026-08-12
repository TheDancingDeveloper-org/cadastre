#!/usr/bin/env python3
"""Run the synthetic Cadastre stack used by the browser E2E suite.

The process owns a temporary catalog copied from the checked-in synthetic
example, then exposes both product transports on loopback.  It never reads
deployment configuration, credentials, or a live estate.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import TextIO

from cadastre.adapters.http import CadastreHTTPServer
from cadastre.core.storage import import_legacy, initialize
from cadastre.mcp.streamable import MCPHTTPServer


def build_fake_catalog(destination: Path) -> Path:
    """Build an isolated runtime catalog from the synthetic example bundle."""
    source = Path(__file__).resolve().parents[1] / "examples" / "catalog"
    shutil.copytree(source, destination)
    initialize(destination)
    import_legacy(destination, destination)
    return destination


def _origins(api: CadastreHTTPServer, mcp: MCPHTTPServer) -> tuple[str, str]:
    return (
        f"http://127.0.0.1:{api.server_port}",
        f"http://127.0.0.1:{mcp.server_port}/mcp",
    )


def write_environment(
    destination: TextIO, api: CadastreHTTPServer, mcp: MCPHTTPServer
) -> None:
    """Publish shell-safe loopback origins for the browser runner."""
    api_origin, mcp_origin = _origins(api, mcp)
    destination.write(f"CADASTRE_E2E_API_ORIGIN={api_origin}\n")
    destination.write(f"CADASTRE_E2E_MCP_ORIGIN={mcp_origin}\n")
    destination.flush()


def run(api_port: int, mcp_port: int, *, environment_file: Path | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="cadastre-e2e-") as temporary:
        root = build_fake_catalog(Path(temporary) / "catalog")
        api = CadastreHTTPServer(
            ("127.0.0.1", api_port),
            root,
            allow_write=False,
            allowed_origins=("http://127.0.0.1:5173",),
        )
        mcp = MCPHTTPServer(("127.0.0.1", mcp_port), root, require_auth=False)
        threads = [
            threading.Thread(target=api.serve_forever, daemon=True),
            threading.Thread(target=mcp.serve_forever, daemon=True),
        ]
        for thread in threads:
            thread.start()

        stop = threading.Event()

        def shutdown(_signum: int, _frame: object) -> None:
            stop.set()
            api.shutdown()
            mcp.shutdown()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        api_origin, mcp_origin = _origins(api, mcp)
        if environment_file is not None:
            environment_file.parent.mkdir(parents=True, exist_ok=True)
            with environment_file.open("w", encoding="utf-8") as destination:
                write_environment(destination, api, mcp)
        print(
            f"cadastre-e2e-ready api={api_origin} mcp={mcp_origin}",
            flush=True,
        )
        stop.wait()
        for thread in threads:
            thread.join(timeout=5)
        api.server_close()
        mcp.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-port",
        type=int,
        default=0,
        help="loopback API port; zero asks the OS for an unused port",
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=0,
        help="loopback MCP port; zero asks the OS for an unused port",
    )
    parser.add_argument(
        "--environment-file",
        type=Path,
        help="write the selected HTTP and MCP origins for the browser runner",
    )
    args = parser.parse_args(argv)
    return run(
        args.api_port,
        args.mcp_port,
        environment_file=args.environment_file,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
