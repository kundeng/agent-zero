"""``haz uninstall-service`` — counterpart to ``install-service``.

Thin re-export module: the LazyGroup dispatch in ``hyperagent0.cli``
maps each subcommand name to a single module that must expose a
``command`` symbol. Both install and uninstall live in the same logic
module (``install_service``); this file just re-exposes the uninstall
command under the right symbol name.
"""

from .install_service import uninstall_command as command  # noqa: F401
