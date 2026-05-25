"""Bridge from the provision framework to ``usr/secrets.env`` (spec 08 D6).

Provisioners write platform tokens through this module rather than
touching :class:`python.helpers.secrets.SecretsManager` directly.
Three reasons:

1. **Allow-list enforcement.** Each provisioner declares
   ``required_secrets``. The bridge refuses to write any key outside
   that list — a programmer error that surfaces immediately rather
   than silently leaving stray secrets in the env file.

2. **Lazy upstream import.** Importing :mod:`python.helpers.secrets`
   pulls in ``dotenv`` and a chain of upstream helpers. Keeping that
   import inside the bridge methods keeps :mod:`hyperagent0.channels.provision`
   itself cheap (the cold-start budget from spec 03 D5 applies to
   ``haz`` startup — provisioning code is only reachable from
   subcommands that explicitly invoke it).

3. **Non-destructive merge.** :meth:`SecretsManager.save_secrets_with_merge`
   deletes keys that don't appear in the submitted text. That's a
   poor fit for the "write two new keys, leave the other 14 alone"
   pattern provisioners actually need. The bridge does a targeted
   update-or-append on the raw env file and calls the (non-merging)
   :meth:`SecretsManager.save_secrets` so the cache stays in sync.

The bridge keeps no state beyond the allow-list, so a fresh instance
per provisioning session is fine.
"""

from __future__ import annotations

from typing import Iterable, Optional


def _escape_env_value(value: str) -> str:
    """Escape a value for safe inclusion as ``KEY="VALUE"`` in an env file.

    Slack tokens use only ``[A-Za-z0-9._-]`` so the escape is rarely
    exercised in practice, but bot display names and channel ids can
    carry arbitrary characters — better to be careful here than to
    discover the bug when someone provisions with a quote in the name.
    """

    return value.replace("\\", "\\\\").replace('"', '\\"')


class AllowlistedSecretsBridge:
    """Concrete :class:`SecretsBridge` for a single provisioner.

    Instantiated by the Flask handler (or the CLI shim) with the
    target provisioner's :attr:`BaseProvisioner.required_secrets`.
    Writes through it are filtered against that list.
    """

    def __init__(
        self,
        required_secrets: Iterable[str],
        *,
        bot_name: str = "",
    ) -> None:
        # Spec 09 task 1.14: when ``bot_name`` names a non-legacy bot,
        # the allow-list becomes the per-bot suffixed forms so two bots
        # on the same platform hold independent tokens. Legacy/empty
        # bot names keep the bare keys (strangler-fig contract).
        from .config import secret_key_for_bot

        self._allowed = {
            secret_key_for_bot(bot_name, k).upper() for k in required_secrets
        }

    # ------------------------------------------------------------------
    # SecretsBridge protocol implementation
    # ------------------------------------------------------------------

    def write(self, values: dict[str, str]) -> None:
        if not values:
            return
        rejected = sorted(k for k in values if k.upper() not in self._allowed)
        if rejected:
            raise ValueError(
                f"AllowlistedSecretsBridge refuses to write keys outside "
                f"this provisioner's required_secrets: {rejected}. "
                f"Add them to the provisioner's class attribute or fix "
                f"the typo."
            )

        from python.helpers.secrets import SecretsManager  # lazy

        mgr = SecretsManager.get_instance()
        existing_raw = mgr.read_secrets_raw() or ""
        # Trailing newline normalization — keep the file ending in '\n'
        # for diff cleanliness without depending on the upstream
        # serializer's behavior.
        lines = existing_raw.splitlines()

        # Map existing keys to their line index so we can update in
        # place. Skip comments and blanks. Keys are case-folded to
        # match SecretsManager's "uppercase" canonical form.
        key_to_index: dict[str, int] = {}
        for i, raw_line in enumerate(lines):
            stripped = raw_line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in raw_line:
                continue
            key_part = raw_line.split("=", 1)[0].strip()
            if key_part:
                key_to_index[key_part.upper()] = i

        for key, value in values.items():
            key_upper = key.upper()
            new_line = f'{key_upper}="{_escape_env_value(value)}"'
            idx = key_to_index.get(key_upper)
            if idx is not None:
                lines[idx] = new_line
            else:
                lines.append(new_line)
                key_to_index[key_upper] = len(lines) - 1

        new_content = "\n".join(lines)
        if not new_content.endswith("\n"):
            new_content += "\n"

        # save_secrets() writes raw + invalidates the cache. The merge
        # variant would delete unrelated keys — don't use it here.
        mgr.save_secrets(new_content)

    def read(self, key: str) -> Optional[str]:
        """Return the cleartext value of a previously-written key.

        Reads bypass the allow-list (a provisioner may legitimately
        want to read a value it wrote in a prior step). Returns
        ``None`` if the key is absent.
        """

        from python.helpers.secrets import SecretsManager  # lazy

        mgr = SecretsManager.get_instance()
        secrets = mgr.load_secrets()
        return secrets.get(key.upper())
