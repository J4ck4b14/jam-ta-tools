"""Blender extension entry point for JAM TA Tools."""

from . import ta_tools

bl_info = ta_tools.bl_info


def register():
    ta_tools.register()


def unregister():
    ta_tools.unregister()
