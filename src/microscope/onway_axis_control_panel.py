"""Assembles AxisControlPanel from the core/camera/scan mixins."""

from src.microscope.onway_axis_core import AxisControlCore
from src.microscope.onway_axis_camera import AxisCameraMixin
from src.microscope.onway_axis_scan import AxisScanMixin


class AxisControlPanel(AxisScanMixin, AxisCameraMixin, AxisControlCore):
    """Complete axis / motion control panel (core + camera + wafer-scan mixins)."""
    pass
