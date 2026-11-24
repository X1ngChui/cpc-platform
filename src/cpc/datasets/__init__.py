"""数据集：清单、获取、校验、加载与变换（负载、电网信号、功率特性）。见 cpc.datasets.base。"""

from .base import (
    DATASETS, BundledFile, Dataset, DatasetError, Manifest, RemoteFile, all_datasets, data_dir, make_dataset, register_dataset,
)

__all__ = ["DATASETS", "BundledFile", "Dataset", "DatasetError", "Manifest", "RemoteFile", "all_datasets", "data_dir",
           "make_dataset", "register_dataset"]
