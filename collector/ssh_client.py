import logging
import time

import paramiko

logger = logging.getLogger(__name__)


class RouterSSHClient:
    """
    Thin wrapper around paramiko that maintains a persistent SSH connection
    and automatically reconnects on failure.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        connect_timeout: int = 15,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.connect_timeout = connect_timeout
        self._client: paramiko.SSHClient | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=self.connect_timeout,
            allow_agent=False,
            look_for_keys=False,
            banner_timeout=30,
        )
        self._client = client
        logger.info("SSH connected to %s:%d", self.host, self.port)

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run(self, command: str, timeout: int = 60) -> str:
        """
        Execute *command* on the remote host and return stdout as a string.
        Retries once with a fresh connection on SSH errors.
        """
        for attempt in range(2):
            try:
                if self._client is None:
                    self.connect()
                _, stdout, _ = self._client.exec_command(command, timeout=timeout)  # type: ignore[union-attr]
                return stdout.read().decode(errors="replace")
            except (paramiko.SSHException, OSError) as exc:
                logger.warning(
                    "SSH exec failed on %s (attempt %d/2): %s",
                    self.host,
                    attempt + 1,
                    exc,
                )
                self.close()
                if attempt == 1:
                    raise
                time.sleep(2)
        return ""  # unreachable, but satisfies type checkers
