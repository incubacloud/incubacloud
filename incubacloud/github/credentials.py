from dataclasses import dataclass, field


@dataclass
class GitHubAppCredentials:
    """Immutable value object carrying GitHub App credentials.

    The private key is never included in logs or repr output.
    Installation tokens derived from these credentials must not be
    persisted to the database — use the in-memory token cache instead.
    """

    app_id: str
    installation_id: str
    private_key_pem: str          # Never logged or returned to clients
    webhook_secret: str = field(default="")

    def __repr__(self) -> str:
        return (
            f"GitHubAppCredentials("
            f"app_id={self.app_id!r}, "
            f"installation_id={self.installation_id!r}, "
            f"private_key_pem=<redacted>, "
            f"webhook_secret=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()
