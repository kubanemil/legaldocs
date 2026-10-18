import logging
import os
import socketserver
from pathlib import Path

from nsm.device import MockNsm
from nsm.measure import measure
from protocol import nsm_api

log = logging.getLogger("nsm")


class NsmServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, device: MockNsm) -> None:
        self.device = device
        super().__init__(path, _Handler)


class _Handler(socketserver.BaseRequestHandler):
    server: NsmServer

    def handle(self) -> None:
        self.request.settimeout(5)
        try:
            request = nsm_api.receive(self.request)
            nsm_api.send(self.request, self.server.device.handle(request))
        except (nsm_api.NsmError, ValueError, OSError) as exc:
            log.warning("dropped NSM request: %s", type(exc).__name__)


def publish(path: Path, content: bytes) -> None:
    staging = path.with_suffix(".tmp")
    staging.write_bytes(content)
    staging.replace(path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    socket_path = Path(os.environ.get("NSM_SOCKET", "/run/nsm/nsm.sock"))
    trust_anchor = Path(os.environ.get("NSM_TRUST_ANCHOR", "/trust/nsm-root.pem"))
    pcrs = measure(Path(os.environ.get("NSM_MEASURED_DIR", "/measured")))

    device = MockNsm(pcrs)
    publish(trust_anchor, device.root_pem)
    socket_path.unlink(missing_ok=True)
    server = NsmServer(str(socket_path), device)
    socket_path.chmod(0o660)
    log.info("mock NSM ready module_id=%s pcr0=%s", device.module_id, pcrs[0].hex())
    server.serve_forever()


if __name__ == "__main__":
    main()
