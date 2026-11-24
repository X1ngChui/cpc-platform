"""数据集：描述文件（Data Package v2 + cpc 类别规范）、校验与列式加载。

平台只接受符合类别规范的数据集（见 profiles.py）；原始数据到平台格式的转换由用户完成，范例见 examples/datasets/。
"""

from .descriptor import DatasetError, Descriptor, Field, Resource, data_dir, expand, load_descriptor
from .profiles import KINDS, PROFILE_VERSION

__all__ = ["KINDS", "PROFILE_VERSION", "DatasetError", "Descriptor", "Field", "Resource", "data_dir", "expand", "load_descriptor"]
