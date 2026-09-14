# Dev Fleet builtin app.
#
# Two halves. The fleet backend (``server.py``) is a SPAWNED, sandboxed process the
# gateway reverse-proxies at ``/apps/dev-fleet/api/``. The live-target cutover is
# NOT in it: that runs in the gateway process, under ``/api/apps/dev-fleet/``, via
# the ``register_routes`` the ``BUILTIN_NAMES`` loop in ``dashboard/routes/system.py``
# picks up here (``importlib.import_module("kiro_crew.apps.builtins.dev_fleet")``
# then ``_mod.register_routes(app)``) — see ``gateway_routes.py`` for why.
from .gateway_routes import register_routes  # noqa: F401
