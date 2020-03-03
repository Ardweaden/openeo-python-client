import copy
import datetime
import logging
import pathlib
import typing
from typing import List, Dict, Union, Tuple

import shapely.geometry
import shapely.geometry.base
from deprecated import deprecated
from shapely.geometry import Polygon, MultiPolygon, mapping

from openeo.imagecollection import ImageCollection, CollectionMetadata
from openeo.internal.graphbuilder import GraphBuilder
from openeo.job import Job
from openeo.util import get_temporal_extent, dict_no_none

if hasattr(typing, 'TYPE_CHECKING') and typing.TYPE_CHECKING:
    # Only import this for type hinting purposes. Runtime import causes circular dependency issues.
    # Note: the `hasattr` check is necessary for Python versions before 3.5.2.
    from openeo.rest.connection import Connection


log = logging.getLogger(__name__)


# TODO #108 this file is still full of "ImageCollection" references (type hints, docs, isinstance checks, ...)

class DataCube(ImageCollection):
    """
    Class representing a Data Cube.

    Supports openEO API 1.0.
    In earlier versions this was called ImageCollection
    """

    def __init__(self, builder: GraphBuilder, connection: 'Connection', metadata: CollectionMetadata = None):
        super().__init__(metadata=metadata)
        self.builder = builder
        self._connection = connection
        self.metadata = metadata

    def __str__(self):
        return "DataCube: %s" % self.builder.result_node["process_id"]

    @property
    def graph(self):
        """Flattened process graph representation"""
        return self.builder.flatten()

    @property
    def _api_version(self):
        return self._connection.capabilities().api_version_check

    @classmethod
    def load_collection(
            cls, collection_id: str, connection: 'Connection' = None,
            spatial_extent: Union[Dict[str, float], None] = None,
            temporal_extent: Union[List[Union[str, datetime.datetime, datetime.date]], None] = None,
            bands: Union[List[str], None] = None,
            fetch_metadata=True
    ):
        """
        Create a new Raster Data cube.

        :param collection_id: A collection id, should exist in the backend.
        :param connection: The connection to use to connect with the backend.
        :param spatial_extent: limit data to specified bounding box or polygons
        :param temporal_extent: limit data to specified temporal interval
        :param bands: only add the specified bands
        :return:
        """
        # TODO: rename function to load_collection for better similarity with corresponding process id?
        builder = GraphBuilder()
        process_id = 'load_collection'
        normalized_temporal_extent = list(
            get_temporal_extent(extent=temporal_extent)) if temporal_extent is not None else None
        arguments = {
            'id': collection_id,
            'spatial_extent': spatial_extent,
            'temporal_extent': normalized_temporal_extent,
        }
        if bands:
            arguments['bands'] = bands
        builder.add_process(process_id, arguments=arguments)
        metadata = connection.collection_metadata(collection_id) if fetch_metadata else None
        if bands:
            metadata.filter_bands(bands)
        return cls(builder=builder, connection=connection, metadata=metadata)

    @classmethod
    @deprecated("use load_collection instead")
    def create_collection(cls, *args, **kwargs):
        return cls.load_collection(*args, **kwargs)

    @classmethod
    def load_disk_collection(cls, connection: 'Connection', file_format: str, glob_pattern: str,
                             **options) -> 'ImageCollection':
        """
        Loads image data from disk as an ImageCollection.

        :param connection: The connection to use to connect with the backend.
        :param file_format: the file format, e.g. 'GTiff'
        :param glob_pattern: a glob pattern that matches the files to load from disk
        :param options: options specific to the file format
        :return: the data as an ImageCollection
        """
        builder = GraphBuilder()

        process_id = 'load_disk_data'
        arguments = {
            'format': file_format,
            'glob_pattern': glob_pattern,
            'options': options
        }

        builder.add_process(process_id, arguments=arguments)
        return cls(builder=builder, connection=connection, metadata={})

    def _filter_temporal(self, start: str, end: str) -> 'ImageCollection':
        return self.graph_add_process(
            process_id='filter_temporal',
            args={
                'data': {'from_node': self.builder.result_node},
                'extent': [start, end]
            }
        )

    def filter_bbox(self, west, east, north, south, crs=None, base=None, height=None) -> 'ImageCollection':
        extent = {
            'west': west, 'east': east, 'north': north, 'south': south,
            'crs': crs,
        }
        if base is not None or height is not None:
            extent.update(base=base, height=height)
        return self.graph_add_process(
            process_id='filter_bbox',
            args={
                'data': {'from_node': self.builder.result_node},
                'extent': extent
            }
        )

    def filter_bands(self, bands: List[Union[str, int]]) -> 'DataCube':
        """Filter the imagery by the given bands
            :param bands: List of band names or single band name as a string.
            :return An ImageCollection instance
        """
        new_collection = self.graph_add_process(process_id='filter_bands',
                                                args={'data': {'from_node': self.builder.result_node}, 'bands': bands})
        if new_collection.metadata is not None:
            new_collection.metadata.filter_bands(bands)
        return new_collection

    @deprecated("use `filter_bands()` instead")
    def band_filter(self, bands) -> ImageCollection:
        return self.filter_bands(bands)

    def band(self, band: Union[str, int]) -> 'ImageCollection':
        """Filter the imagery by the given bands
            :param band: band name, band common name or band index.
            :return An ImageCollection instance
        """

        process_id = 'reduce'  # TODO #124 reduce_dimension/reduce_dimension_binary
        band_index = self.metadata.get_band_index(band)

        args = {
            'data': {'from_node': self.builder.result_node},
            # TODO #116 hardcoded dimension name
            'dimension': 'spectral_bands',
            'reducer': {
                'callback': {
                    'arguments': {
                        'data': {
                            'from_argument': 'data'
                        },
                        'index': band_index
                    },
                    'process_id': 'array_element',
                    'result': True
                }
            }
        }

        return self.graph_add_process(process_id, args)

    def resample_spatial(self, resolution: Union[float, Tuple[float, float]],
                         projection: Union[int, str] = None, method: str = 'near', align: str = 'upper-left'):
        return self.graph_add_process('resample_spatial', {
            'data': {'from_node': self.builder.result_node},
            'resolution': resolution,
            'projection': projection,
            'method': method,
            'align': align
        })

    def subtract(self, other: Union['DataCube', int, float], reverse=False):
        """
        Subtract other from this datacube, so the result is: this - other
        The number of bands in both data cubes has to be the same.

        :param other:
        :return ImageCollection: this - other
        """
        operator = "subtract"
        if isinstance(other, (int, float)):
            return self._reduce_bands_binary_const(operator, other, reverse=reverse)
        elif isinstance(other, DataCube):
            return self._reduce_bands_binary(operator, other)
        else:
            raise ValueError("Unsupported right-hand operand: " + str(other))

    def divide(self, other: Union[ImageCollection, Union[int, float]]):
        """
        Subtraction other from this datacube, so the result is: this - other
        The number of bands in both data cubes has to be the same.

        :param other:
        :return ImageCollection: this - other
        """
        operator = "divide"
        if isinstance(other, (int, float)):
            return self._reduce_bands_binary_const(operator, other)
        elif isinstance(other, DataCube):
            return self._reduce_bands_binary(operator, other)
        else:
            raise ValueError("Unsupported right-hand operand: " + str(other))

    def product(self, other: Union['DataCube', int, float], reverse=False):
        """
        Multiply other with this datacube, so the result is: this * other
        The number of bands in both data cubes has to be the same.

        :param other:
        :return ImageCollection: this - other
        """
        operator = "product"
        if isinstance(other, (int, float)):
            return self._reduce_bands_binary_const(operator, other, reverse=reverse)
        elif isinstance(other, DataCube):
            return self._reduce_bands_binary(operator, other)
        else:
            raise ValueError("Unsupported right-hand operand: " + str(other))

    def logical_or(self, other: ImageCollection):
        """
        Apply element-wise logical `or` operation
        :param other:
        :return ImageCollection: logical_or(this, other)
        """
        return self._reduce_bands_binary(operator='or', other=other, arg_name='expressions')

    def logical_and(self, other: ImageCollection):
        """
        Apply element-wise logical `and` operation
        :param other:
        :return ImageCollection: logical_and(this, other)
        """
        return self._reduce_bands_binary(operator='and', other=other, arg_name='expressions')

    def __invert__(self):
        """

        :return:
        """
        operator = 'not'
        my_builder = self._get_band_graph_builder()
        new_builder = None
        extend_previous_callback_graph = my_builder is not None
        # TODO: why does these `add_process` calls use "expression" instead of "data" like the other cases?
        if not extend_previous_callback_graph:
            new_builder = GraphBuilder()
            # TODO merge both process graphs?
            new_builder.add_process(operator, expression={'from_argument': 'data'})
        else:
            new_builder = my_builder.shallow_copy()
            new_builder.result_node['result'] = False
            new_builder.add_process(operator, expression={'from_node': new_builder.result_node})

        return self._create_reduced_collection(new_builder, extend_previous_callback_graph)

    def __ne__(self, other: Union[ImageCollection, Union[int, float]]):
        return self.__eq__(other).__invert__()

    def __eq__(self, other: Union[ImageCollection, Union[int, float]]):
        """
        Pixelwise comparison of this data cube with another cube or constant.

        :param other: Another data cube, or a constant
        :return:
        """
        return self._reduce_bands_binary_xy('eq', other)

    def __gt__(self, other: Union[ImageCollection, Union[int, float]]):
        """
        Pairwise comparison of the bands in this data cube with the bands in the 'other' data cube.
        The number of bands in both data cubes has to be the same.

        :param other:
        :return ImageCollection: this + other
        """
        return self._reduce_bands_binary_xy('gt', other)

    def __lt__(self, other: Union[ImageCollection, Union[int, float]]):
        """
        Pairwise comparison of the bands in this data cube with the bands in the 'other' data cube.
        The number of bands in both data cubes has to be the same.

        :param other:
        :return ImageCollection: this + other
        """
        return self._reduce_bands_binary_xy('lt', other)

    def _create_reduced_collection(self, callback_graph_builder, extend_previous_callback_graph):
        if not extend_previous_callback_graph:
            # there was no previous reduce step
            log.warning("Doing band math without proper `DataCube.band()` usage. There is probably something wrong. See issue #123")
            args = {
                'data': {'from_node': self.builder.result_node},
                # TODO: avoid hardcoded dimension name 'spectral_bands' #116
                'dimension': 'spectral_bands',
                'reducer': {
                    'callback': callback_graph_builder.result_node
                }
            }
            return self.graph_add_process("reduce", args)  # TODO #124 reduce_dimension/reduce_dimension_binary
        else:
            process_graph_copy = self.builder.shallow_copy()
            process_graph_copy.result_node['arguments']['reducer']['callback'] = callback_graph_builder.result_node

            # now current_node should be a reduce node, let's modify it
            # TODO: set metadata of reduced cube?
            return DataCube(builder=process_graph_copy, connection=self._connection)

    def __truediv__(self, other):
        return self.divide(other)

    def __add__(self, other):
        return self.add(other)

    def __radd__(self, other):
        return self.add(other, reverse=True)

    def __sub__(self, other):
        return self.subtract(other)

    def __rsub__(self, other):
        return self.subtract(other, reverse=True)

    def __mul__(self, other):
        return self.product(other)

    def __rmul__(self, other):
        return self.product(other, reverse=True)

    def __or__(self, other):
        return self.logical_or(other)

    def __and__(self, other):
        return self.logical_and(other)

    def add(self, other: Union['DataCube', int, float], reverse=False):
        """
        Pairwise addition of the bands in this data cube with the bands in the 'other' data cube.
        The number of bands in both data cubes has to be the same.

        :param other:
        :return ImageCollection: this + other
        """
        operator = "sum"
        if isinstance(other, (int, float)):
            return self._reduce_bands_binary_const(operator, other, reverse=reverse)
        elif isinstance(other, DataCube):
            return self._reduce_bands_binary(operator, other)
        else:
            raise ValueError("Unsupported right-hand operand: " + str(other))

    def _reduce_bands_binary(self, operator, other: 'DataCube', arg_name='data'):
        # first we create the callback
        fallback_node = GraphBuilder.from_process_graph({'from_argument': 'data'})
        my_builder = self._get_band_graph_builder()
        other_builder = other._get_band_graph_builder()
        merged = GraphBuilder.combine(operator=operator,
                                      first=my_builder or fallback_node,
                                      second=other_builder or fallback_node, arg_name=arg_name)
        # callback is ready, now we need to properly set up the reduce process that will invoke it
        if my_builder is None and other_builder is None:
            # there was no previous reduce step, perhaps this is a cube merge?
            # cube merge is happening when node id's differ, otherwise we can use regular reduce
            if (self.builder.result_node != other.builder.result_node):
                # we're combining data from two different datacubes: http://api.openeo.org/v/0.4.0/processreference/#merge_cubes

                # set result node id's first, to keep track
                my_builder = self.builder
                other_builder = other.builder

                cubes_merged = GraphBuilder.combine(operator="merge_cubes",
                                                    first=my_builder,
                                                    second=other_builder, arg_name="cubes")

                the_node = cubes_merged.result_node

                cubes = the_node["arguments"]["cubes"]
                the_node["arguments"]["cube1"] = cubes[0]
                the_node["arguments"]["cube2"] = cubes[1]
                del the_node["arguments"]["cubes"]

                # there can be only one process for now
                cube_list = merged.result_node["arguments"][arg_name]
                assert len(cube_list) == 2
                # it is really not clear if this is the agreed way to go
                cube_list[0]["from_argument"] = "cube1"
                cube_list[1]["from_argument"] = "cube2"
                del cube_list[0]["from_node"]
                del cube_list[1]["from_node"]
                the_node["arguments"]["overlap_resolver"] = {
                    'callback': merged.result_node
                }
                return DataCube(builder=cubes_merged, connection=self._connection, metadata=self.metadata)
            else:
                args = {
                    'data': {'from_node': self.builder.result_node},
                    'reducer': {
                        'callback': merged.processes
                    }
                }
                return self.graph_add_process("reduce", args) # TODO #124 reduce_dimension/reduce_dimension_binary
        else:

            reducing_graph = self
            if reducing_graph.builder.result_node["process_id"] != "reduce":  # TODO #124 reduce_dimension/reduce_dimension_binary
                reducing_graph = other
            new_builder = reducing_graph.builder.shallow_copy()
            new_builder.result_node['arguments']['reducer']['callback'] = merged.result_node
            # now current_node should be a reduce node, let's modify it
            # TODO: set metadata of reduced cube?
            return DataCube(builder=new_builder, connection=reducing_graph._connection)

    def _reduce_bands_binary_xy(self, operator, other: Union[ImageCollection, Union[int, float]]):
        """
        Pixelwise comparison of this data cube with another cube or constant.

        :param other: Another data cube, or a constant
        :return:
        """
        if isinstance(other, int) or isinstance(other, float):
            my_builder = self._get_band_graph_builder()
            new_builder = None
            extend_previous_callback_graph = my_builder is not None
            if not extend_previous_callback_graph:
                new_builder = GraphBuilder()
                # TODO merge both process graphs?
                new_builder.add_process(operator, x={'from_argument': 'data'}, y=other)
            else:
                new_builder = my_builder.shallow_copy()
                new_builder.result_node['result'] = False
                new_builder.add_process(operator, x={'from_node': new_builder.result_node}, y=other)

            return self._create_reduced_collection(new_builder, extend_previous_callback_graph)
        elif isinstance(other, ImageCollection):
            return self._reduce_bands_binary(operator, other)
        else:
            raise ValueError("Unsupported right-hand operand: " + str(other))

    def _reduce_bands_binary_const(self, operator, other: Union[int, float], reverse=False):
        my_callback_builder = self._get_band_graph_builder()

        extend_previous_callback_graph = my_callback_builder is not None
        if not extend_previous_callback_graph:
            new_callback_builder = GraphBuilder()
            data = [{'from_argument': 'data'}, other]
        else:
            new_callback_builder = my_callback_builder
            data = [{'from_node': new_callback_builder.result_node}, other]
        if reverse:
            data = list(reversed(data))
        new_callback_builder.add_process(operator, data=data)

        return self._create_reduced_collection(new_callback_builder, extend_previous_callback_graph)

    def _get_band_graph_builder(self):
        """Get process graph builder of "spectral" reduce callback if available"""
        current_node = self.builder.result_node
        if current_node["process_id"] == "reduce":  # TODO #124 reduce_dimension/reduce_dimension_binary
            # TODO: avoid hardcoded "spectral_bands" dimension #76 #93 #116
            if current_node["arguments"]["dimension"] == "spectral_bands":
                callback_graph = current_node["arguments"]["reducer"]["callback"]
                return GraphBuilder.from_process_graph(callback_graph)
        return None

    def zonal_statistics(self, regions, func, scale=1000, interval="day") -> 'ImageCollection':
        """Calculates statistics for each zone specified in a file.
            :param regions: GeoJSON or a path to a GeoJSON file containing the
                            regions. For paths you must specify the path to a
                            user-uploaded file without the user id in the path.
            :param func: Statistical function to calculate for the specified
                         zones. example values: min, max, mean, median, mode
            :param scale: A nominal scale in meters of the projection to work
                          in. Defaults to 1000.
            :param interval: Interval to group the time series. Allowed values:
                            day, wee, month, year. Defaults to day.
            :return: An ImageCollection instance
        """
        regions_geojson = regions
        if isinstance(regions, Polygon) or isinstance(regions, MultiPolygon):
            regions_geojson = mapping(regions)
        process_id = 'zonal_statistics'
        args = {
            'data': {'from_node': self.builder.result_node},
            'regions': regions_geojson,
            'func': func,
            'scale': scale,
            'interval': interval
        }

        return self.graph_add_process(process_id, args)

    def apply_dimension(self, code: str, runtime=None, version="latest", dimension='temporal') -> 'ImageCollection':
        """
        Applies an n-ary process (i.e. takes an array of pixel values instead of a single pixel value) to a raster data cube.
        In contrast, the process apply applies an unary process to all pixel values.

        By default, apply_dimension applies the the process on all pixel values in the data cube as apply does, but the parameter dimension can be specified to work only on a particular dimension only. For example, if the temporal dimension is specified the process will work on a time series of pixel values.

        The n-ary process must return as many elements in the returned array as there are in the input array. Otherwise a CardinalityChanged error must be returned.


        :param code: UDF code or process identifier
        :param runtime:
        :param version:
        :param dimension:
        :return:
        :raises: CardinalityChangedError
        """
        process_id = 'apply_dimension'
        if runtime:
            callback = {
                'udf': self._create_run_udf(code, runtime, version)
            }
        else:
            callback = {
                "arguments": {
                    "data": {
                        "from_argument": "data"
                    }
                },
                "process_id": code,
                "result": True
            }
        args = {
            'data': {
                'from_node': self.builder.result_node
            },
            'dimension': dimension,
            'process': {
                'callback': callback
            }
        }
        return self.graph_add_process(process_id, args)

    def apply_tiles(self, code: str, runtime="Python", version="latest") -> 'ImageCollection':
        """Apply a function to the given set of tiles in this image collection.

            This type applies a simple function to one pixel of the input image or image collection.
            The function gets the value of one pixel (including all bands) as input and produces a single scalar or tuple output.
            The result has the same schema as the input image (collection) but different bands.
            Examples include the computation of vegetation indexes or filtering cloudy pixels.

            Code should follow the OpenEO UDF conventions.

            TODO: Deprecated since 0.4.0?

            :param code: String representing Python code to be executed in the backend.
        """
        process_id = 'reduce'  # TODO #124 reduce_dimension/reduce_dimension_binary
        args = {
            'data': {
                'from_node': self.builder.result_node
            },
            'dimension': 'spectral_bands',  # TODO determine dimension based on datacube metadata
            'binary': False,
            'reducer': {
                'callback': self._create_run_udf(code, runtime, version)
            }
        }
        return self.graph_add_process(process_id, args)

    def _create_run_udf(self, code, runtime, version):
        return {
            "arguments": {
                "data": {
                    "from_argument": "data"
                },
                "runtime": runtime,
                "version": version,
                "udf": code

            },
            "process_id": "run_udf",
            "result": True
        }

    # TODO better name, pull to ABC?
    def reduce_tiles_over_time(self, code: str, runtime="Python", version="latest"):
        """
        Applies a user defined function to a timeseries of tiles. The size of the tile is backend specific, and can be limited to one pixel.
        The function should reduce the given timeseries into a single (multiband) tile.

        :param code: The UDF code, compatible with the given runtime and version
        :param runtime: The UDF runtime
        :param version: The UDF runtime version
        :return:
        """
        process_id = 'reduce'  # TODO #124 reduce_dimension/reduce_dimension_binary
        args = {
            'data': {
                'from_node': self.builder.result_node
            },
            'dimension': 'temporal',  # TODO determine dimension based on datacube metadata
            'binary': False,
            'reducer': {
                'callback': {
                    'udf': self._create_run_udf(code, runtime, version)
                }
            }
        }
        return self.graph_add_process(process_id, args)

    def apply(self, process: str, data_argument='data', arguments={}) -> 'ImageCollection':
        process_id = 'apply'
        arguments[data_argument] = \
            {
                "from_argument": data_argument
            }
        args = {
            'data': {'from_node': self.builder.result_node},
            'process': {
                'callback': {
                    "unary": {
                        "arguments": arguments,
                        "process_id": process,
                        "result": True
                    }
                }
            }
        }

        return self.graph_add_process(process_id, args)

    def _reduce_time(self, reduce_function="max"):
        process_id = 'reduce'  # TODO #124 reduce_dimension/reduce_dimension_binary

        args = {
            'data': {'from_node': self.builder.result_node},
            'dimension': 'temporal',
            'reducer': {
                'callback': {
                    'r1': {
                        'arguments': {
                            'data': {
                                'from_argument': 'data'
                            }
                        },
                        'process_id': reduce_function,
                        'result': True
                    }
                }
            }
        }

        return self.graph_add_process(process_id, args)

    def min_time(self) -> 'ImageCollection':
        """Finds the minimum value of a time series for all bands of the input dataset.

            :return: An ImageCollection instance
        """

        return self._reduce_time(reduce_function="min")

    def max_time(self) -> 'ImageCollection':
        """
        Finds the maximum value of a time series for all bands of the input dataset.

        :return: An ImageCollection instance
        """
        return self._reduce_time(reduce_function="max")

    def mean_time(self) -> 'ImageCollection':
        """Finds the mean value of a time series for all bands of the input dataset.

            :return: An ImageCollection instance
        """
        return self._reduce_time(reduce_function="mean")

    def median_time(self) -> 'ImageCollection':
        """Finds the median value of a time series for all bands of the input dataset.

            :return: An ImageCollection instance
        """

        return self._reduce_time(reduce_function="median")

    def count_time(self) -> 'ImageCollection':
        """Counts the number of images with a valid mask in a time series for all bands of the input dataset.

            :return: An ImageCollection instance
        """
        return self._reduce_time(reduce_function="count")

    def ndvi(self, nir: str = None, red: str = None, target_band: str = None) -> 'ImageCollection':
        """ Normalized Difference Vegetation Index (NDVI)

            :param nir: name of NIR band
            :param red: name of red band
            :param target_band: (optional) name of the newly created band

            :return: An ImageCollection instance
        """
        return self.graph_add_process(
            process_id='ndvi',
            args=dict_no_none(
                data={'from_node': self.builder.result_node},
                nir=nir, red=red, target_band=target_band
            )
        )

    @deprecated("use 'linear_scale_range' instead")
    def stretch_colors(self, min, max) -> 'ImageCollection':
        """ Color stretching
        deprecated, use 'linear_scale_range' instead

            :param min: Minimum value
            :param max: Maximum value
            :return: An ImageCollection instance
        """
        process_id = 'stretch_colors'
        args = {
            'data': {'from_node': self.builder.result_node},
            'min': min,
            'max': max
        }

        return self.graph_add_process(process_id, args)

    def linear_scale_range(self, input_min, input_max, output_min, output_max) -> 'ImageCollection':
        """ Color stretching
            :param input_min: Minimum input value
            :param input_max: Maximum input value
            :param output_min: Minimum output value
            :param output_max: Maximum output value
            :return An ImageCollection instance
        """
        process_id = 'linear_scale_range'
        args = {
            'x': {'from_node': self.builder.result_node},
            'inputMin': input_min,
            'inputMax': input_max,
            'outputMin': output_min,
            'outputMax': output_max
        }
        return self.graph_add_process(process_id, args)

    def mask(self, mask: 'DataCube' = None, replacement=None) -> 'DataCube':
        """
        Applies a mask to a raster data cube. To apply a vector mask use `mask_polygon`.

        A mask is a raster data cube for which corresponding pixels among `data` and `mask`
        are compared and those pixels in `data` are replaced whose pixels in `mask` are non-zero
        (for numbers) or true (for boolean values).
        The pixel values are replaced with the value specified for `replacement`,
        which defaults to null (no data).

        :param mask: the raster mask
        :param replacement: the value to replace the masked pixels with
        """
        return self.graph_add_process(
            process_id="mask",
            args=dict_no_none(
                data={'from_node': self.builder.result_node},
                mask={'from_node': mask.builder.result_node},
                replacement=replacement
            )
        )

    def mask_polygon(
            self, mask: Union[Polygon, MultiPolygon, str, pathlib.Path] = None,
            srs="EPSG:4326", replacement=None, inside: bool = None
    ) -> 'DataCube':
        """
        Applies a polygon mask to a raster data cube. To apply a raster mask use `mask`.

        All pixels for which the point at the pixel center does not intersect with any
        polygon (as defined in the Simple Features standard by the OGC) are replaced.
        This behaviour can be inverted by setting the parameter `inside` to true.

        The pixel values are replaced with the value specified for `replacement`,
        which defaults to `no data`.

        :param mask: A polygon, provided as a :class:`shapely.geometry.Polygon` or :class:`shapely.geometry.MultiPolygon`, or a filename pointing to a valid vector file
        :param srs: The reference system of the provided polygon, by default this is Lat Lon (EPSG:4326).
        :param replacement: the value to replace the masked pixels with
        """
        if isinstance(mask, (str, pathlib.Path)):
            # TODO: default to loading file client side?
            # TODO: change read_vector to load_uploaded_files https://github.com/Open-EO/openeo-processes/pull/106
            read_vector = self.graph_add_process(
                process_id='read_vector',
                args={'filename': str(mask)}
            )
            mask = {'from_node': read_vector.builder.result_node}
        elif isinstance(mask, shapely.geometry.base.BaseGeometry):
            if mask.area == 0:
                raise ValueError("Mask {m!s} has an area of {a!r}".format(m=mask, a=mask.area))
            mask = shapely.geometry.mapping(mask)
            mask['crs'] = {
                'type': 'name',
                'properties': {'name': srs}
            }
        else:
            # Assume mask is already a valid GeoJSON object
            assert "type" in mask

        return self.graph_add_process(
            process_id="mask",
            args=dict_no_none(
                data={"from_node": self.builder.result_node},
                mask=mask,
                replacement=replacement,
                inside=inside
            )
        )

    def merge(self, other: 'DataCube') -> 'DataCube':
        # TODO: overlap_resolver parameter
        # TODO provide this as a GraphBuilder method?
        builder = GraphBuilder()
        builder.add_process(
            process_id="merge_cubes",
            arguments={
                'cube1': {'from_node': self.builder.result_node},
                'cube2': {'from_node': other.builder.result_node},
            }
        )
        # TODO: metadata?
        return DataCube(builder=builder, connection=self._connection, metadata=None)

    def apply_kernel(self, kernel, factor=1.0) -> 'ImageCollection':
        """
        Applies a focal operation based on a weighted kernel to each value of the specified dimensions in the data cube.

        :param kernel: The kernel to be applied on the data cube. It should be a 2D numpy array.
        :param factor: A factor that is multiplied to each value computed by the focal operation. This is basically a shortcut for explicitly multiplying each value by a factor afterwards, which is often required for some kernel-based algorithms such as the Gaussian blur.
        :return: A data cube with the newly computed values. The resolution, cardinality and the number of dimensions are the same as for the original data cube.
        """
        return self.graph_add_process('apply_kernel', {
            'data': {'from_node': self.builder.result_node},
            'kernel': kernel.tolist(),
            'factor': factor
        })

    ####VIEW methods #######

    def polygonal_mean_timeseries(self, polygon: Union[Polygon, MultiPolygon, str]) -> 'ImageCollection':
        """
        Extract a mean time series for the given (multi)polygon. Its points are
        expected to be in the EPSG:4326 coordinate
        reference system.

        :param polygon: The (multi)polygon; or a file path or HTTP URL to a GeoJSON file or shape file
        :return: ImageCollection
        """

        return self._polygonal_timeseries(polygon, "mean")

    def polygonal_histogram_timeseries(self, polygon: Union[Polygon, MultiPolygon, str]) -> 'ImageCollection':
        """
        Extract a histogram time series for the given (multi)polygon. Its points are
        expected to be in the EPSG:4326 coordinate
        reference system.

        :param polygon: The (multi)polygon; or a file path or HTTP URL to a GeoJSON file or shape file
        :return: ImageCollection
        """

        return self._polygonal_timeseries(polygon, "histogram")

    def polygonal_median_timeseries(self, polygon: Union[Polygon, MultiPolygon, str]) -> 'ImageCollection':
        """
        Extract a median time series for the given (multi)polygon. Its points are
        expected to be in the EPSG:4326 coordinate
        reference system.

        :param polygon: The (multi)polygon; or a file path or HTTP URL to a GeoJSON file or shape file
        :return: ImageCollection
        """

        return self._polygonal_timeseries(polygon, "median")

    def polygonal_standarddeviation_timeseries(self, polygon: Union[Polygon, MultiPolygon, str]) -> 'ImageCollection':
        """
        Extract a time series of standard deviations for the given (multi)polygon. Its points are
        expected to be in the EPSG:4326 coordinate
        reference system.

        :param polygon: The (multi)polygon; or a file path or HTTP URL to a GeoJSON file or shape file
        :return: ImageCollection
        """

        return self._polygonal_timeseries(polygon, "sd")

    def _polygonal_timeseries(self, polygon: Union[Polygon, MultiPolygon, str], func: str) -> 'ImageCollection':
        def graph_add_aggregate_process(graph) -> 'ImageCollection':
            process_id = 'aggregate_polygon'
            args = {
                'data': {'from_node': self.builder.result_node},
                'polygons': polygons,
                'reducer': {
                    'callback': {
                        "arguments": {
                            "data": {
                                "from_argument": "data"
                            }
                        },
                        "process_id": func,
                        "result": True
                    }
                }
            }
            return graph.graph_add_process(process_id, args)

        if isinstance(polygon, str):
            with_read_vector = self.graph_add_process('read_vector', args={
                'filename': polygon
            })
            polygons = {
                'from_node': with_read_vector.builder.result_node
            }
            return graph_add_aggregate_process(with_read_vector)
        else:
            polygons = mapping(polygon)
            polygons['crs'] = {
                'type': 'name',
                'properties': {
                    'name': 'EPSG:4326'
                }
            }
            return graph_add_aggregate_process(self)

    def save_result(self, format: str = "GTIFF", options: dict = None):
        return self.graph_add_process(
            process_id="save_result",
            args={
                "data": {"from_node": self.builder.result_node},
                "format": format,
                "options": options or {}
            }
        )

    def download(self, outputfile: str, format: str = "GTIFF", options: dict = None):
        """Download image collection, e.g. as GeoTIFF."""
        newcollection = self.save_result(format=format, options=options)
        newcollection.builder.result_node['result'] = True
        return self._connection.download(newcollection.builder.flatten(), outputfile)

    def tiled_viewing_service(self, **kwargs) -> Dict:
        return self._connection.create_service(self.builder.flatten(), **kwargs)

    def execute_batch(
            self,
            outputfile: Union[str, pathlib.Path], out_format: str = None,
            print=print, max_poll_interval=60, connection_retry_interval=30,
            job_options=None, **format_options):
        """
        Evaluate the process graph by creating a batch job, and retrieving the results when it is finished.
        This method is mostly recommended if the batch job is expected to run in a reasonable amount of time.

        For very long running jobs, you probably do not want to keep the client running.

        :param job_options:
        :param outputfile: The path of a file to which a result can be written
        :param out_format: String Format of the job result.
        :param format_options: String Parameters for the job result format

        """
        from openeo.rest.job import RESTJob
        job = self.send_job(out_format, job_options=job_options, **format_options)
        return RESTJob.run_synchronous(
            job, outputfile,
            print=print, max_poll_interval=max_poll_interval, connection_retry_interval=connection_retry_interval
        )

    def send_job(self, out_format=None, job_options=None, **format_options) -> Job:
        """
        Sends a job to the backend and returns a ClientJob instance.

        :param out_format: String Format of the job result.
        :param job_options:
        :param format_options: String Parameters for the job result format
        :return: status: ClientJob resulting job.
        """
        img = self
        if out_format:
            # add `save_result` node
            img = img.save_result(format=out_format, options=format_options)
        img.graph[img.node_id]["result"] = True
        return self._connection.create_job(process_graph=img.graph, additional=job_options)

    def execute(self) -> Dict:
        """Executes the process graph of the imagery. """
        newbuilder = self.builder.shallow_copy()
        newbuilder.result_node['result'] = True
        return self._connection.execute({"process_graph": newbuilder.flatten()}, "")

    ####### HELPER methods #######

    def graph_add_process(self, process_id, args) -> 'DataCube':
        """
        Returns a new imagecollection with an added process with the given process
        id and a dictionary of arguments

        :param process_id: String, Process Id of the added process.
        :param args: Dict, Arguments of the process.

        :return: new ImageCollectionClient instance
        """
        # don't modify in place, return new builder
        newbuilder = self.builder.shallow_copy()
        newbuilder.add_process(process_id, arguments=args)

        # TODO: properly update metadata as well?
        newCollection = DataCube(builder=newbuilder, connection=self._connection, metadata=copy.copy(self.metadata))
        return newCollection

    def to_graphviz(self):
        """
        Build a graphviz DiGraph from the process graph
        :return:
        """
        import graphviz
        import pprint

        graph = graphviz.Digraph(node_attr={"shape": "none", "fontname": "sans", "fontsize": "11"})
        for name, process in self.graph.items():
            args = process.get("arguments", {})
            # Build label
            label = '<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0">'
            label += '<TR><TD COLSPAN="2" BGCOLOR="#eeeeee">{pid}</TD></TR>'.format(pid=process["process_id"])
            label += "".join(
                '''<TR><TD ALIGN="RIGHT">{arg}</TD>
                       <TD ALIGN="LEFT"><FONT FACE="monospace">{value}</FONT></TD></TR>'''.format(
                    arg=k, value=pprint.pformat(v)[:1000].replace('\n', '<BR/>')
                ) for k, v in sorted(args.items())
            )
            label += '</TABLE>>'
            # Add node and edges to graph
            graph.node(name, label=label)
            if "data" in args and "from_node" in args["data"]:
                graph.edge(args["data"]["from_node"], name)

            # TODO: add subgraph for "callback" arguments?

        return graph
