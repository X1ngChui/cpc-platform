"""Datasets: descriptors (Data Package v2 + cpc kind specifications), validation and columnar loading.

The platform accepts datasets that follow a kind specification (see profiles.py); converting raw data
into this format is up to the user, with examples in examples/datasets/.
"""

from .descriptor import DatasetError, Descriptor, Field, Resource, data_dir, expand, load_descriptor
from .profiles import KINDS, PROFILE_VERSION

__all__ = ["KINDS", "PROFILE_VERSION", "DatasetError", "Descriptor", "Field", "Resource", "data_dir", "expand", "load_descriptor"]
