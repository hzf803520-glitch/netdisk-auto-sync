# -*- coding: utf-8 -*-
"""
云盘适配器模块
支持多网盘的统一接口
"""

from app.extensions.adapters.baidu_adapter import BaiduAdapter
from app.extensions.adapters.base_adapter import BaseCloudDriveAdapter
from app.extensions.adapters.quark_adapter import QuarkAdapter
from app.extensions.adapters.uc_adapter import UCAdapter
from app.extensions.adapters.xunlei_adapter import XunleiAdapter

__all__ = [
    "BaseCloudDriveAdapter",
    "QuarkAdapter",
    "BaiduAdapter",
    "XunleiAdapter",
    "UCAdapter",
]

__version__ = "2.0.0"
