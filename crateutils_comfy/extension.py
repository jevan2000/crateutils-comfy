"""ComfyUI V3 entry point: registers the nodes, the trigger routes and the trigger thread."""

from __future__ import annotations

import logging

from comfy_api.latest import ComfyExtension, IO
from typing_extensions import override

from . import __version__, nodes_export, nodes_files, nodes_notify, nodes_triggers


class CrateUtilsComfy(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[IO.ComfyNode]]:
        try:
            from . import comfy_glue

            comfy_glue.add_routes()
            comfy_glue.manager()  # restores armed triggers and starts the trigger thread
        except Exception as e:  # never block the nodes from loading
            logging.warning("CrateUtils-Comfy: triggers unavailable: %s", e)
        logging.info("CrateUtils-Comfy %s loaded", __version__)
        return nodes_triggers.NODES + nodes_files.NODES + nodes_export.NODES + nodes_notify.NODES


async def comfy_entrypoint() -> CrateUtilsComfy:
    return CrateUtilsComfy()
