"""
@author: Gabriele Girelli
@contact: gigi.ga90@gmail.com
"""

import argparse
from collections import defaultdict
from czifile import CziFile  # type: ignore
import logging
from logging import Logger, getLogger
from nd2reader import ND2Reader  # type: ignore
from nd2reader.parser import Parser as ND2Parser  # type: ignore
import numpy as np  # type: ignore
from radiantkit import stat
from radiantkit.string import TIFFNameTemplate as TNTemplate
import re
import six  # type: ignore
from typing import DefaultDict, Iterable, List, Optional, Tuple
import warnings
import xml.etree.ElementTree as ET
from collections import Counter
import re
import mmap
from typing import List, Tuple
from functools import lru_cache
import os
import nd2


_RE_Z_LOOP_STEP = re.compile(
    "Z Stack Loop: ([0-9]+)\r\n- Step: ([0-9,\\.]+) ([^\r\n]*)"
)

class ND2Reader2(ND2Reader):
    _xy_resolution: float
    _z_resolution: DefaultDict[float, int]
    _dtype: str

    def __init__(self, filename):
        super(ND2Reader2, self).__init__(filename)  
        self._set_xy_resolution()
        with nd2.ND2File(self.filename) as f:
            check3d = f.sizes
        if 'Z' in check3d:
            self._set_z_resolution()
        else:
            self._z_resolution = {}
        self._set_proposed_dtype()
        self._z_loop_step_cache = {}

    def log_details(self, logger: Logger = getLogger()) -> None:
        logger.info(f"Input: '{self.filename}'")

        logger.info(
            "".join(
                [
                    f"Found {self.field_count()} field(s) of view, ",
                    f"with {self.channel_count()} channel(s).",
                ]
            )
        )

        logger.info(f"Channels: {list(self.get_channel_names())}.")

        if self.is3D():
            logger.info(
                "".join(
                    [
                        f"XYZ size: {self.sizes['x']} x ",
                        f"{self.sizes['y']} x {self.sizes['z']}",
                    ]
                )
            )
            logger.info(f"XY resolution: {self.xy_resolution:.3f} um")
            logger.info(f"Delta Z value(s): {list(self._z_resolution.keys())} um")
        else:
            logger.info(f"XY size: {self.sizes['x']} x {self.sizes['y']}")
            logger.info(f"XY resolution: {self.xy_resolution} um")

        logger.info(
            f"Format: '{self.dtype}' [{self.pixel_type_tag}:{self.bits_per_pixel}]"
        )

    @property
    def xy_resolution(self) -> float:
        return self._xy_resolution

    @property
    def z_resolution(self) -> List[Tuple[float, int]]:
        return list(self._z_resolution.items())

    @property
    def z_resolution_mode(self) -> float:
        return stat.get_hist_mode(list(self._z_resolution.items()))

    @property
    def pixel_type_tag(self) -> int:
        return self.parser._raw_metadata.image_attributes[six.b("SLxImageAttributes")][
            six.b("ePixelType")
        ]

    @property
    def bits_per_pixel(self) -> int:
        return self.parser._raw_metadata.image_attributes[six.b("SLxImageAttributes")][
            six.b("uiBpcInMemory")
        ]

    @property
    def dtype(self) -> str:
        return self._dtype

    def _set_xy_resolution(self):
        self._xy_resolution = self.metadata["pixel_microns"]
        if 0 == self._xy_resolution:
            logging.warning("XY resolution set to 0! (possibly incorrect obj. setup)")

    #original
    # def _set_z_resolution(self):
    #    self._z_resolution: DefaultDict[float, int] = defaultdict(lambda: 0)
    #    for field_id in range(self.field_count()):
    #        for delta_z in self.get_field_resolutionZ(field_id):
    #            self._z_resolution[delta_z] += 1

    #improvement
    def _set_z_resolution(self):
        delta_z_list = [delta_z for field_id in range(self.field_count())
            for delta_z in self.get_field_resolutionZ(field_id)]
        self._z_resolution = Counter(delta_z_list)

    def _set_proposed_dtype(self) -> None:
        dtype_tag: DefaultDict = defaultdict(lambda: "float")
        dtype_tag[1] = "uint"
        dtype_tag[2] = "int"
        dtype = f"{dtype_tag[self.pixel_type_tag]}{self.bits_per_pixel}"
        supported_dtypes = (
            "uint8",
            "uint16",
            "uint32",
            "int8",
            "int16",
            "int32",
            "float8",
            "float16",
            "float32",
        )
        self._dtype = "float64" if dtype not in supported_dtypes else dtype

    def field_count(self) -> int:
        if "v" not in self.axes:
            return 1
        return self.sizes["v"]

    def isLive(self) -> bool:
        if "t" in self.axes:
            return 1 < self.sizes["t"]
        return False

    def is3D(self) -> bool:
        return "z" in self.axes

    def has_multi_channels(self) -> bool:
        if "c" in self.axes:
            return 1 < self.channel_count()
        return False

    def get_channel_names(self) -> Iterable[str]:
        for channel in self.metadata["channels"]:
            yield channel.lower()

    def channel_count(self) -> int:
        if "c" not in self.sizes:
            n = 1
        else:
            n = self.sizes["c"]
        assert len(list(self.get_channel_names())) == n, "channel count mismatch."
        return n

    def set_axes_for_bundling(self):
        if self.is3D():
            self.bundle_axes = "zyxc" if self.has_multi_channels() else "zyx"
        else:
            self.bundle_axes = "yxc" if "c" in self.axes else "yx"


    #improvement        
    @lru_cache(maxsize=128)
    def get_Z_loop_step(self, parser) -> Tuple[int, float, str]:
        image_text_info = parser._raw_metadata.image_text_info[
            six.b("SLxImageTextInfo")
        ]
        metadata_fields = [x for x in image_text_info.values() if b"Z Stack Loop" in x]
        if 0 == len(metadata_fields):
            return (0, np.nan, "")
        metadata = metadata_fields[0]
        parsed = _RE_Z_LOOP_STEP.search(metadata.decode())
        if parsed is None:
            return (0, np.nan, "")
        parsed_fields = parsed.groups()
        return (
            int(parsed_fields[0]),
            float(parsed_fields[1].replace(",", ".")),
            parsed_fields[2],
        )

    def get_field_resolutionZ(self, field_id: int) -> List[float]:
        with open(self.filename, "rb") as ND2H:
            size = os.path.getsize(self.filename)
            with mmap.mmap(ND2H.fileno(), size, access=mmap.ACCESS_READ) as mem:
                parser = ND2Parser(mem)
                z_fields, z_step, z_unit = self.get_Z_loop_step(parser)
                if 0 != z_fields:
                    return [z_step] 
                Zdata = np.array(parser._raw_metadata.z_data)
                Zlevels = np.array(parser.metadata["z_levels"]).astype("int")
                Zlevels = Zlevels + len(Zlevels) * field_id
                Zdata = Zdata[Zlevels]
                return np.round(np.diff(Zdata), 3).tolist()
            
    #original
    # def get_Z_loop_step(self, parser) -> Tuple[int, float, str]:
    #     image_text_info = parser._raw_metadata.image_text_info[
    #         six.b("SLxImageTextInfo")
    #     ]
    #     metadata_fields = [x for x in image_text_info.values() if b"Z Stack Loop" in x]
    #     if 0 == len(metadata_fields):
    #         return (0, np.nan, "")
    #     metadata = metadata_fields[0]
    #     parsed = re.search(
    #         "Z Stack Loop: ([0-9]+)\r\n- Step: ([0-9,\\.]+) ([^\r\n]*)",
    #         metadata.decode(),
    #     )
    #     if parsed is None:
    #         return (0, np.nan, "")
    #     parsed_fields = parsed.groups()
    #     return (
    #         int(parsed_fields[0]),
    #         float(parsed_fields[1].replace(",", ".")),
    #         parsed_fields[2],
    #     )


    #original
    # def get_field_resolutionZ(self, field_id: int) -> List[float]:
    #     with open(self.filename, "rb") as ND2H:
    #         parser = ND2Parser(ND2H)
    #         z_fields, z_step, z_unit = self.get_Z_loop_step(parser)
    #         if 0 != z_fields:
    #             return [z_step]
    #         Zdata = np.array(parser._raw_metadata.z_data)
    #         Zlevels = np.array(parser.metadata["z_levels"]).astype("int")
    #         Zlevels = Zlevels + len(Zlevels) * field_id
    #         Zdata = Zdata[Zlevels]
    #         return np.round(np.diff(Zdata), 3).tolist()



    def select_channels(self, channels: List[str]) -> List[str]:
        return [
            c.lower() for c in channels if c.lower() in list(self.get_channel_names())
        ]

    def get_tiff_path(
        self, template: TNTemplate, channel_id: int, field_id: int
    ) -> str:
        d = dict(
            channel_name=self.metadata["channels"][channel_id].lower(),
            channel_id=f"{(channel_id+1):03d}",
            series_id=f"{(field_id+1):05d}",
            dimensions=len(self.bundle_axes),
            axes_order="".join(self.bundle_axes),
        )
        return f"{template.safe_substitute(d)}.tiff"


class CziFile2(CziFile):
    __pixels: Optional[np.ndarray] = None
    axes: str

    def __init__(self, filename):
        super(CziFile2, self).__init__(filename)

    @property
    def pixels(self) -> np.ndarray:
        if self.__pixels is None:
            with warnings.catch_warnings(record=True):
                self.__pixels = self.asarray()
        return self.__pixels

    def log_details(self, logger: Logger = getLogger()) -> None:
        logger.info(f"Input: '{self._fh.name}'")

        logger.info(
            "".join(
                [
                    f"Found {self.field_count()} field(s) of view, ",
                    f"with {self.channel_count()} channel(s).",
                ]
            )
        )

        logger.info(f"Channels: {list(self.get_channel_names())}.")

        x_size = self.pixels.shape[self.axes.index("X")]
        y_size = self.pixels.shape[self.axes.index("Y")]
        if self.is3D:
            z_size = self.pixels.shape[self.axes.index("Z")]
            logger.info(f"XYZ size: {x_size} x {y_size} x {z_size}")
        else:
            logger.info(f"XY size: {x_size} x {y_size}")

        for axis_name, axis_resolution in self.get_resolution().items():
            logger.info(f"{axis_name} resolution: {axis_resolution*1e6:.3f} um")

    def field_count(self) -> int:
        if "S" not in self.axes:
            return 1
        return self.pixels.shape[self.axes.index("S")]

    def isLive(self) -> bool:
        if "T" in self.axes:
            return 1 < self.shape[self.axes.index("T")]
        return False

    def is3D(self) -> bool:
        return "Z" in self.axes

    def has_multi_channels(self) -> bool:
        if "C" in self.axes:
            return 1 < self.channel_count()
        return False

    def get_channel_names(self) -> Iterable[str]:
        channel_path = "Metadata/DisplaySetting/Channels/Channel/DyeName"
        for x in ET.fromstring(self.metadata()).findall(channel_path):
            if x.text is None:
                continue
            yield x.text.replace(" ", "").lower()

    def channel_count(self) -> int:
        if "C" not in self.axes:
            n = 1
        else:
            n = self.pixels.shape[self.axes.index("C")]
        assert len(list(self.get_channel_names())) == n, "channel count mismatch."
        return n

    def get_axis_resolution(self, axis: str) -> float:
        resolution_path = "Metadata/Scaling/Items/Distance"
        for x in ET.fromstring(self.metadata()).findall(resolution_path):
            if x.attrib["Id"] == axis:
                if x[0].text is not None:
                    return float(x[0].text)
        return 1

    def get_resolution(self) -> dict:
        resolution = {}
        for axis in "XYZ":
            resolution[axis] = self.get_axis_resolution(axis)
        return resolution

    def squeeze_axes(self, skip: str) -> None:
        axes = list(self.axes)
        for axis in axes:
            axis_id = axes.index(axis)
            if axis in skip:
                continue
            self.__pixels = np.squeeze(self.pixels, axis_id)
            self.shape = self.pixels.shape
            axes.pop(axis_id)
        self.axes = "".join(axes)

    def reorder_axes(self, bundle_axes: str) -> None:
        if self.axes == bundle_axes:
            return
        bundle_axes_list = [a for a in bundle_axes if a in self.axes]
        assert len(bundle_axes_list) == len(self.axes)
        assert all([axis in self.axes for axis in bundle_axes_list])
        self.__pixels = np.moveaxis(
            self.pixels,
            range(len(self.axes)),
            [bundle_axes_list.index(axis) for axis in self.axes],
        )
        self.shape = self.pixels.shape
        self.axes = "".join(bundle_axes_list)

    def get_channel_pixels(
        self, args: argparse.Namespace, field_id: Optional[int] = None
    ) -> Iterable[Tuple[np.ndarray, int]]:
        if field_id is not None:
            field = self.pixels[field_id, :]
        else:
            assert "C" == self.axes[0]
            field = self.pixels
        for channel_id in range(self.channel_count()):
            yield (field[channel_id], channel_id)

    def select_channels(self, channels: List[str]) -> List[str]:
        return [
            c.lower() for c in channels if c.lower() in list(self.get_channel_names())
        ]

    def get_tiff_path(
        self, template: TNTemplate, channel_id: int, field_id: int
    ) -> str:
        d = dict(
            channel_name=list(self.get_channel_names())[channel_id],
            channel_id=f"{(channel_id+1):03d}",
            series_id=f"{(field_id+1):05d}",
            dimensions=3,
            axes_order="ZYX",
        )
        return f"{template.safe_substitute(d)}.tiff"
