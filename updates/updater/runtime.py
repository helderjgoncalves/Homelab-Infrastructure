"""Docker Engine over the socket, and Compose through the container config.toml names."""

from __future__ import annotations

import io
import json
import os
import posixpath
import shutil
import socket
import tarfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path

API = "1.41"
# updates/: config.toml, .env, icons/ and state/ sit next to this package.
HERE = Path(__file__).resolve().parents[1]
STACKS = HERE.parent
SOCKET = "/var/run/docker.sock"
PULL_ATTEMPTS = 3
PULL_PAUSE = 30


class DockerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Stack:
    """A Compose project, as Compose labels the containers it starts."""

    project: str
    files: tuple[str, ...]
    directory: str


@dataclass(frozen=True)
class Container:
    name: str
    image: str
    image_id: str
    version: str
    status: str
    health: str
    restarts: int = 0
    service: str = ""
    # None when Compose did not start the container.
    stack: Stack | None = None


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip() and "Error loading config file" not in line:
            return line.strip()[:300]
    return ""


def rate_limited(error: str) -> bool:
    text = error.lower()
    return "toomanyrequests" in text or "too many requests" in text


def retry_rate_limit(what: str, action) -> None:
    """A registry that answers "too many requests" usually lets the same pull through a moment later."""
    for attempt in range(1, PULL_ATTEMPTS + 1):
        try:
            action()
            return
        except DockerError as exc:
            if attempt == PULL_ATTEMPTS or not rate_limited(str(exc)):
                raise
            log(f"{what} was rate limited by the registry, trying again in {PULL_PAUSE} seconds")
            time.sleep(PULL_PAUSE)


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
    def __init__(self, socket_path: str = SOCKET, compose_container: str = ""):
        self.socket_path = socket_path
        self.compose_container = compose_container
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
        return Container(
            name, image, payload["Image"], version, payload["State"]["Status"], health, int(payload.get("RestartCount") or 0),
            labels.get("com.docker.compose.service") or "", stack_of(labels),
        )

    def archive(self, name: str, path: str) -> bytes:
        """`path` out of a container as a tar, with its owner and mode, for put_archive."""
        query = urllib.parse.urlencode({"path": path})
        return self._read(f"/containers/{urllib.parse.quote(name, safe='')}/archive?{query}")

    def put_archive(self, name: str, path: str, data: bytes) -> None:
        """Put an archive() of `path` back where it came from. The container may be created and not yet started."""
        query = urllib.parse.urlencode({"path": posixpath.dirname(path.rstrip("/")) or "/"})
        status, reply = self._open("PUT", f"/containers/{urllib.parse.quote(name, safe='')}/archive?{query}", data, content_type="application/x-tar")
        if status >= 300:
            raise DockerError(last_line(reply.decode(errors="replace")) or f"could not put {path} back in {name} ({status})")

    def archive_to(self, name: str, path: str, target: Path) -> bool:
        """`path` out of a container into the tar file `target`, streamed, for put_archive_file. False when `path` does not exist."""
        query = urllib.parse.urlencode({"path": path})
        connection = _UnixConnection(self.socket_path, timeout=300)
        try:
            connection.request("GET", f"{self._api()}/containers/{urllib.parse.quote(name, safe='')}/archive?{query}", headers={"Host": "localhost"})
            response = connection.getresponse()
            if response.status == 404:
                response.read()
                return False
            if response.status >= 300:
                raise DockerError(last_line(response.read().decode(errors="replace")) or f"could not copy {path} out of {name} ({response.status})")
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
                shutil.copyfileobj(response, handle, 1 << 20)
            return True
        except (OSError, HTTPException) as exc:
            raise DockerError(str(exc)) from exc
        finally:
            connection.close()

    def put_archive_file(self, name: str, path: str, source: Path) -> None:
        """Put an archive_to() of `path` back where it came from, streamed. The container may be created and not yet started."""
        query = urllib.parse.urlencode({"path": posixpath.dirname(path.rstrip("/")) or "/"})
        connection = _UnixConnection(self.socket_path, timeout=300)
        try:
            with source.open("rb") as handle:
                headers = {"Host": "localhost", "Content-Type": "application/x-tar", "Content-Length": str(source.stat().st_size)}
                connection.request("PUT", f"{self._api()}/containers/{urllib.parse.quote(name, safe='')}/archive?{query}", body=handle, headers=headers)
                response = connection.getresponse()
                reply = response.read()
            if response.status >= 300:
                raise DockerError(last_line(reply.decode(errors="replace")) or f"could not put {path} back in {name} ({response.status})")
        except (OSError, HTTPException) as exc:
            raise DockerError(str(exc)) from exc
        finally:
            connection.close()

    def read_file(self, name: str, path: str) -> bytes:
        """One file out of a container, through the archive API. Nothing runs inside it."""
        data = self.archive(name, path)
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

    def image_version(self, ref: str) -> str:
        """The version label of a local image, before any container runs it. Empty when it has none."""
        payload = self._request("GET", f"/images/{urllib.parse.quote(ref, safe='')}/json")
        return ((payload.get("Config") or {}).get("Labels") or {}).get("org.opencontainers.image.version") or ""

    def pull(self, ref: str) -> None:
        retry_rate_limit(f"pull of {ref}", lambda: self._pull(ref))

    def _pull(self, ref: str) -> None:
        name, tag = split_ref(ref)
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
        name, tag = split_ref(target)
        query = urllib.parse.urlencode({"repo": name, "tag": tag})
        self._request("POST", f"/images/{urllib.parse.quote(source, safe='')}/tag?{query}")

    def prune_unused(self) -> tuple[int, int]:
        payload = self._request("POST", unused_image_prune_path())
        deleted = [item for item in payload.get("ImagesDeleted") or [] if item and item.get("Deleted")]
        return len(deleted), int(payload.get("SpaceReclaimed") or 0)

    def exec(self, container: str, cmd: list[str]) -> tuple[int, str]:
        return self._exec(container, cmd)

    def compose(self, stack: Stack, *args: str) -> None:
        if not self.compose_container:
            raise DockerError("no compose_container in config.toml")

        def once() -> None:
            code, output = self._exec(self.compose_container, ["/usr/bin/docker", *compose_args(stack), *args])
            if code != 0:
                raise DockerError(last_line(output) or f"compose exited {code}")

        if args[:1] == ("pull",):
            retry_rate_limit(f"{stack.project} pull", once)
        else:
            once()

    def compose_config(self, stack: Stack) -> dict:
        """The project as Compose resolves it, .env values included: compared in memory, never logged or sent."""
        if not self.compose_container:
            raise DockerError("no compose_container in config.toml")
        code, stdout, stderr = self._exec_streams(self.compose_container, ["/usr/bin/docker", *compose_args(stack), "config", "--format", "json"])
        if code != 0:
            raise DockerError(last_line(stderr) or f"compose config exited {code}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DockerError(f"compose config did not print JSON: {exc}") from exc

    def ensure_image(self, image: str) -> None:
        """Pull `image` when it is not here, such as after the Sunday prune."""
        try:
            self._request("GET", f"/images/{urllib.parse.quote(image, safe='')}/json")
        except DockerError:
            name, _, digest = image.partition("@")
            query = urllib.parse.urlencode({"fromImage": name, "tag": digest} if digest else {"fromImage": image})
            status, data = self._open("POST", f"/images/create?{query}", None)
            if status >= 300 or b'"error"' in data:
                raise DockerError(last_line(data.decode(errors="replace")) or f"pull of {image} failed ({status})") from None

    def run_once(self, image: str, cmd: list[str], *, entrypoint: list[str] | None = None, user: str = "", workdir: str = "",
                 env: list[str] | None = None, binds: list[str] | None = None, volumes_from: list[str] | None = None) -> tuple[int, str]:
        """A one-shot container with no network, removed after it exits. `image` is pulled first when missing."""
        self.ensure_image(image)
        spec: dict = {
            "Image": image,
            "Cmd": cmd,
            "HostConfig": {"NetworkMode": "none", "Binds": binds or [], "VolumesFrom": volumes_from or []},
        }
        if entrypoint is not None:
            spec["Entrypoint"] = entrypoint
        if user:
            spec["User"] = user
        if workdir:
            spec["WorkingDir"] = workdir
        if env:
            spec["Env"] = env
        created = self._request("POST", "/containers/create", spec)
        container_id = created["Id"]
        try:
            self._request("POST", f"/containers/{container_id}/start")
            waited = self._request("POST", f"/containers/{container_id}/wait")
            logs = _demux_text(self._read(f"/containers/{container_id}/logs?stdout=1&stderr=1"))
            return int(waited.get("StatusCode") or 0), logs
        finally:
            try:
                self._request("DELETE", f"/containers/{container_id}?force=1")
            except DockerError:
                pass

    def recreate_runner(self, image: str, stack: Stack) -> None:
        """Compose from a one-shot container, so the compose container is not recreating itself."""
        name = f"updates-{stack.project}-recreate"
        try:
            self._request("DELETE", f"/containers/{name}?force=1")
        except DockerError:
            pass
        created = self._request("POST", f"/containers/create?name={name}", {
            "Image": image,
            "Entrypoint": ["/usr/bin/docker"],
            "Cmd": [*compose_args(stack), "up", "-d", "--pull", "never"],
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
                raise DockerError(last_line(_demux_text(logs)) or f"{stack.project} recreate exited {waited['StatusCode']}")
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
        code, raw = self._exec_raw(container, cmd)
        return code, _demux_text(raw)

    def _exec_streams(self, container: str, cmd: list[str]) -> tuple[int, str, str]:
        code, raw = self._exec_raw(container, cmd)
        stdout, stderr = _demux(raw)
        return code, stdout, stderr

    def _exec_raw(self, container: str, cmd: list[str]) -> tuple[int, bytes]:
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
        return (code if isinstance(code, int) else 1), raw

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

    def _open(self, method: str, path: str, body: bytes | None, prefix: str | None = None, content_type: str = "application/json") -> tuple[int, bytes]:
        if prefix is None:
            prefix = self._api()
        connection = _UnixConnection(self.socket_path, timeout=300)
        headers = {"Host": "localhost", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
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


def stack_of(labels: dict[str, str]) -> Stack | None:
    project = labels.get("com.docker.compose.project") or ""
    files = tuple(path for path in (labels.get("com.docker.compose.project.config_files") or "").split(",") if path)
    directory = labels.get("com.docker.compose.project.working_dir") or ""
    if not (project and files and directory):
        return None
    return Stack(project, files, directory)


def compose_args(stack: Stack) -> list[str]:
    """Every file the project was started with: given -f, Compose reads no override file by itself."""
    files = [part for path in stack.files for part in ("-f", path)]
    return ["compose", "--project-directory", stack.directory, "-p", stack.project, *files]


def split_ref(ref: str) -> tuple[str, str]:
    name, separator, tag = ref.rpartition(":")
    if not separator or "/" in tag:
        return ref, "latest"
    return name, tag


def _demux_text(data: bytes) -> str:
    streams = _split_streams(data)
    if streams is None:
        return data.decode(errors="replace")
    return (streams[0] + b"\n" + streams[1]).decode(errors="replace")


def _demux(data: bytes) -> tuple[str, str]:
    """Docker's multiplexed stream as (stdout, stderr). A stream that is not multiplexed is all stdout."""
    streams = _split_streams(data)
    if streams is None:
        return data.decode(errors="replace"), ""
    return streams[0].decode(errors="replace"), streams[1].decode(errors="replace")


def _split_streams(data: bytes) -> tuple[bytes, bytes] | None:
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
        return None
    return bytes(stdout), bytes(stderr)
