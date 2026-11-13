"""Docker Engine over the socket, and compose through the Dockge container."""

from __future__ import annotations

import io
import json
import socket
import tarfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path

API = "1.41"
HERE = Path(__file__).resolve().parent
STACKS = HERE.parent
SOCKET = "/var/run/docker.sock"


class DockerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Container:
    name: str
    image: str
    image_id: str
    version: str
    status: str
    health: str
    restarts: int = 0


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip() and "Error loading config file" not in line:
            return line.strip()[:300]
    return ""


def api_prefix(version_info: dict) -> str:
    """The API version to speak: ours, unless the daemon no longer accepts it."""
    lowest = str(version_info.get("MinAPIVersion") or "")
    if lowest and _api_number(lowest) > _api_number(API):
        return f"/v{lowest}"
    return f"/v{API}"


def _api_number(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return (0,)


class Docker:
    def __init__(self, socket_path: str = SOCKET):
        self.socket_path = socket_path
        self._prefix: str | None = None

    def names(self) -> set[str]:
        payload = self._request("GET", "/containers/json")
        return {item["Names"][0].lstrip("/") for item in payload}

    def inspect(self, name: str) -> Container:
        payload = self._request("GET", f"/containers/{urllib.parse.quote(name, safe='')}/json")
        labels = payload["Config"].get("Labels") or {}
        version = labels.get("org.opencontainers.image.version") or ""
        image = payload["Config"]["Image"]
        if not version:
            version = image.rsplit(":", 1)[-1].split("@", 1)[0]
        health = (payload.get("State", {}).get("Health") or {}).get("Status") or "none"
        return Container(name, image, payload["Image"], version, payload["State"]["Status"], health, int(payload.get("RestartCount") or 0))

    def read_file(self, name: str, path: str) -> bytes:
        """One file out of a container, through the archive API. Nothing runs inside it."""
        query = urllib.parse.urlencode({"path": path})
        data = self._read(f"/containers/{urllib.parse.quote(name, safe='')}/archive?{query}")
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive:
                    handle = archive.extractfile(member) if member.isfile() else None
                    if handle is not None:
                        return handle.read()
        except tarfile.TarError as exc:
            raise DockerError(f"could not read {path} from {name}: {exc}") from exc
        raise DockerError(f"{path} not found in {name}")

    def image_id(self, ref: str) -> str:
        payload = self._request("GET", f"/images/{urllib.parse.quote(ref, safe='')}/json")
        return payload["Id"]

    def pull(self, ref: str) -> None:
        name, tag = _split_ref(ref)
        query = urllib.parse.urlencode({"fromImage": name, "tag": tag})
        status, data = self._open("POST", f"/images/create?{query}", None)
        text = data.decode(errors="replace")
        if status >= 300:
            raise DockerError(last_line(text) or f"pull of {ref} failed ({status})")
        for line in text.splitlines():
            if '"error"' not in line:
                continue
            try:
                message = json.loads(line).get("error") or line
            except json.JSONDecodeError:
                message = line
            raise DockerError(str(message)[:300])

    def tag(self, source: str, target: str) -> None:
        name, tag = _split_ref(target)
        query = urllib.parse.urlencode({"repo": name, "tag": tag})
        self._request("POST", f"/images/{urllib.parse.quote(source, safe='')}/tag?{query}")

    def prune_unused(self) -> tuple[int, int]:
        payload = self._request("POST", unused_image_prune_path())
        deleted = [item for item in payload.get("ImagesDeleted") or [] if item and item.get("Deleted")]
        return len(deleted), int(payload.get("SpaceReclaimed") or 0)

    def exec(self, container: str, cmd: list[str]) -> tuple[int, str]:
        return self._exec(container, cmd)

    def compose(self, project: str, file: Path, *args: str) -> None:
        code, output = self._exec("dockge", [
            "/usr/bin/docker", "compose",
            "--project-directory", str(file.parent),
            "-p", project,
            "-f", str(file),
            *args,
        ])
        if code != 0:
            raise DockerError(last_line(output) or f"compose exited {code}")

    def recreate_dockge(self, image: str, file: Path) -> None:
        """Compose from a one-shot container, so Dockge is not recreating itself."""
        try:
            self._request("DELETE", "/containers/updates-dockge-recreate?force=1")
        except DockerError:
            pass
        created = self._request("POST", "/containers/create?name=updates-dockge-recreate", {
            "Image": image,
            "Entrypoint": ["/usr/bin/docker"],
            "Cmd": [
                "compose", "--project-directory", str(file.parent),
                "-p", "dockge", "-f", str(file), "up", "-d", "--pull", "never",
            ],
            "HostConfig": {"Binds": [
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{STACKS}:{STACKS}",
            ]},
        })
        container_id = created["Id"]
        try:
            self._request("POST", f"/containers/{container_id}/start")
            waited = self._request("POST", f"/containers/{container_id}/wait")
            logs = self._read(f"/containers/{container_id}/logs?stdout=1&stderr=1")
            if waited.get("StatusCode"):
                raise DockerError(last_line(_demux_text(logs)) or f"dockge recreate exited {waited['StatusCode']}")
        finally:
            try:
                self._request("DELETE", f"/containers/{container_id}?force=1")
            except DockerError:
                pass

    def wait_ready(self, name: str, attempts: int = 24, pause: float = 15) -> bool:
        """Healthy, or with no healthcheck, up for three checks. A restart while waiting is a failure."""
        first_restarts = None
        for attempt in range(1, attempts + 1):
            try:
                container = self.inspect(name)
            except DockerError:
                return False
            if first_restarts is None:
                first_restarts = container.restarts
            if container.restarts > first_restarts:
                log(f"{name} restarted while starting")
                return False
            if container.status != "running":
                log(f"{name} is {container.status}")
                return False
            if container.health == "healthy":
                return True
            if container.health == "none" and attempt >= 3:
                return True
            if container.health == "unhealthy":
                log(f"{name} is unhealthy")
                return False
            time.sleep(pause)
        log(f"{name} was not healthy after 6 minutes")
        return False

    def _exec(self, container: str, cmd: list[str]) -> tuple[int, str]:
        created = self._request("POST", f"/containers/{urllib.parse.quote(container, safe='')}/exec", {
            "AttachStdout": True,
            "AttachStderr": True,
            "Cmd": cmd,
        })
        raw = self._read(f"/exec/{created['Id']}/start", method="POST", payload={"Detach": False, "Tty": False})
        for _attempt in range(20):
            info = self._request("GET", f"/exec/{created['Id']}/json")
            if not info.get("Running") and info.get("ExitCode") is not None:
                break
            time.sleep(0.5)
        code = info.get("ExitCode")
        return (code if isinstance(code, int) else 1), _demux_text(raw)

    def _request(self, method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        status, data = self._open(method, path, body)
        if status >= 300:
            raise DockerError(last_line(data.decode(errors="replace")) or f"docker API {status}")
        if not data:
            return {}
        return json.loads(data)

    def _read(self, path: str, method: str = "GET", payload: dict | None = None) -> bytes:
        body = json.dumps(payload).encode() if payload is not None else None
        status, data = self._open(method, path, body)
        if status >= 300:
            raise DockerError(last_line(data.decode(errors="replace")) or f"docker API {status}")
        return data

    def _api(self) -> str:
        if self._prefix is None:
            status, data = self._open("GET", "/version", None, prefix="")
            if status >= 300:
                raise DockerError(last_line(data.decode(errors="replace")) or f"docker API {status}")
            self._prefix = api_prefix(json.loads(data))
        return self._prefix

    def _open(self, method: str, path: str, body: bytes | None, prefix: str | None = None) -> tuple[int, bytes]:
        if prefix is None:
            prefix = self._api()
        connection = _UnixConnection(self.socket_path, timeout=300)
        headers = {"Host": "localhost", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, prefix + path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        except (OSError, HTTPException) as exc:
            raise DockerError(str(exc)) from exc
        finally:
            connection.close()


class _UnixConnection(HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def unused_image_prune_path() -> str:
    """Every image no container is using. Stopped containers still count as using theirs."""
    filters = json.dumps({"dangling": ["false"]}, separators=(",", ":"))
    return "/images/prune?" + urllib.parse.urlencode({"filters": filters})


def _split_ref(ref: str) -> tuple[str, str]:
    name, separator, tag = ref.rpartition(":")
    if not separator or "/" in tag:
        return ref, "latest"
    return name, tag


def _demux_text(data: bytes) -> str:
    stdout = bytearray()
    stderr = bytearray()
    index = 0
    while index + 8 <= len(data):
        kind = data[index]
        size = int.from_bytes(data[index + 4:index + 8], "big")
        chunk = data[index + 8:index + 8 + size]
        if len(chunk) < size:
            break
        index += 8 + size
        (stdout if kind == 1 else stderr).extend(chunk)
    if index == 0:
        return data.decode(errors="replace")
    return (stdout + b"\n" + stderr).decode(errors="replace")
