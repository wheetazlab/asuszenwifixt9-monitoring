"""
Entry point for the ASUS ZenWiFi XT9 Prometheus exporter.

Usage (inside Docker):
    python -m collector.main

Environment variables are read from config.py (ROUTER_SSH_HOST, SSH_PASSWORD, …).
"""

import logging
import signal
import sys
import time

from prometheus_client import REGISTRY, start_http_server

from .collector import NodeConfig, RouterCollector
from .config import (
    EXTENDER_SSH_HOST,
    EXTENDER_SSH_PORT,
    EXTENDER_TRACKED_INTERFACES,
    EXTENDER_WIFI_IFACES,
    LOG_LEVEL,
    METRICS_PORT,
    ROUTER_BACKHAUL_MACS,
    ROUTER_SSH_HOST,
    ROUTER_SSH_PORT,
    ROUTER_TRACKED_INTERFACES,
    ROUTER_WIFI_IFACES,
    SSH_PASSWORD,
    SSH_USERNAME,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    nodes = [
        NodeConfig(
            name="router",
            host=ROUTER_SSH_HOST,
            port=ROUTER_SSH_PORT,
            username=SSH_USERNAME,
            password=SSH_PASSWORD,
            is_router=True,
            wifi_ifaces=ROUTER_WIFI_IFACES,
            tracked_interfaces=ROUTER_TRACKED_INTERFACES,
            backhaul_macs=set(),
        ),
        NodeConfig(
            name="extender",
            host=EXTENDER_SSH_HOST,
            port=EXTENDER_SSH_PORT,
            username=SSH_USERNAME,
            password=SSH_PASSWORD,
            is_router=False,
            wifi_ifaces=EXTENDER_WIFI_IFACES,
            tracked_interfaces=EXTENDER_TRACKED_INTERFACES,
            # Filter these MAC addresses out of "client" metrics on the extender —
            # they are the router's backhaul radios, not real clients.
            backhaul_macs=ROUTER_BACKHAUL_MACS,
        ),
    ]

    collector = RouterCollector(nodes)
    REGISTRY.register(collector)

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %d — shutting down", signum)
        collector.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "Starting metrics server on :%d  (router=%s  extender=%s)",
        METRICS_PORT,
        ROUTER_SSH_HOST,
        EXTENDER_SSH_HOST,
    )
    start_http_server(METRICS_PORT)
    logger.info("Metrics available at http://0.0.0.0:%d/metrics", METRICS_PORT)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
