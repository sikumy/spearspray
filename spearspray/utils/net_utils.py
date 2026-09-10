import logging
import socket

from spearspray.utils.constants import RED, YELLOW, GREEN, RESET


def is_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, socket.gaierror, ConnectionRefusedError, OSError):
        return False


def check_port_or_exit(host: str, port: int, service: str, skip: bool = False, timeout: float = 3.0, hint: str | None = None) -> None:
    log = logging.getLogger(__name__)

    if skip:
        log.debug(f"[*] Skipping {service} port check on {host}:{port} (--skip-port-check).")
        return

    log.debug(f"[*] Checking {service} port {port} on {host}...")
    if is_port_open(host, port, timeout=timeout):
        log.info(f"{GREEN}[+]{RESET} {service} port {port} open on {host}.")
        return

    message = (
        f"{RED}[-]{RESET} {service} port {port} appears closed or unreachable on {host}. "
        f"Use {YELLOW}--skip-port-check{RESET} to bypass this check if it is a false negative."
    )
    if hint:
        message += f" {hint}"

    log.error(message)
    raise SystemExit(1)
