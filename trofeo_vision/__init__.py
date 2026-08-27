"""Trofeo Vision live dashboard — custom Python renderer + USB driver.

Public entry points:
    from trofeo_vision import Driver, Renderer, SensorReader
"""
from .config import Config
from .driver import Driver, DriverError
from .renderer import Renderer
from .sensors import SensorReader, SensorSnapshot

__version__ = "0.1.0"
__all__ = [
    "Config",
    "Driver",
    "DriverError",
    "Renderer",
    "SensorReader",
    "SensorSnapshot",
]
