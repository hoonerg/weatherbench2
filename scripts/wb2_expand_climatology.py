# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Expand a climatology dataset into forecasts for particular times."""
from collections import abc
import math

from absl import app
from absl import flags
import apache_beam as beam
import pandas as pd
import xarray
import xarray_beam as xbeam


INPUT_PATH = flags.DEFINE_string(
    'input_path',
    None,
    help='path to hourly or daily climatology dataset',
)
OUTPUT_PATH = flags.DEFINE_string(
    'output_path',
    None,
    help='path to save outputs in Zarr format',
)
TIME_START = flags.DEFINE_string(
    'time_start',
    '2017-01-01',
    help='ISO 8601 timestamp (inclusive) at which to start outputs',
)
TIME_STOP = flags.DEFINE_string(
    'time_stop',
    '2017-12-31',
    help='ISO 8601 timestamp (inclusive) at which to stop outputs',
)
TIME_CHUNK_SIZE = flags.DEFINE_integer(
    'time_chunk_size',
    None,
    help='Desired integer chunk size. If not set, inferred from input chunks.',
)
BEAM_RUNNER = flags.DEFINE_string(
    'beam_runner', None, help='beam.runners.Runner'
)


def select_climatology(
    time_slice: slice, climatology: xarray.Dataset, time_index: pd.DatetimeIndex
) -> abc.Iterable[tuple[xbeam.Key, xarray.Dataset]]:
  """Select climatology data matching time_index[time_slice]."""
  chunk_times = time_index[time_slice]
  times_array = xarray.DataArray(
      chunk_times, dims=['time'], coords={'time': chunk_times}
  )
  if 'hour' in climatology.coords:
    chunk = climatology.sel(
        dayofyear=times_array.dt.dayofyear, hour=times_array.dt.hour
    )
    del chunk.coords['dayofyear']
    del chunk.coords['hour']
  else:
    chunk = climatology.sel(dayofyear=times_array.dt.dayofyear)
    del chunk.coords['hour']

  for variable_name in chunk:
    key = xbeam.Key({'time': time_slice.start}, vars={variable_name})
    yield key, chunk[[variable_name]]


def main(_: abc.Sequence[str]) -> None:
  climatology, input_chunks = xbeam.open_zarr(INPUT_PATH.value)

  if 'hour' not in climatology.coords:
    hour_delta = 24
    time_dims = ['dayofyear']
  else:
    hour_delta = (climatology.hour[1] - climatology.hour[0]).item()
    time_dims = ['hour', 'dayofyear']

  times = pd.date_range(
      TIME_START.value, TIME_STOP.value, freq=hour_delta * pd.Timedelta('1h')
  )

  template = (
      xbeam.make_template(climatology)
      .isel({dim: 0 for dim in time_dims}, drop=True)
      .expand_dims(time=times)
  )

  if TIME_CHUNK_SIZE.value is None:
    time_chunk_size = input_chunks['dayofyear'] * input_chunks.get('hour', 1)
  else:
    time_chunk_size = TIME_CHUNK_SIZE.value

  time_chunk_count = math.ceil(times.size / time_chunk_size)

  output_chunks = {dim: -1 for dim in input_chunks if dim not in time_dims}
  output_chunks['time'] = time_chunk_size

  # Beam type checking is broken with Python 3.10:
  # https://github.com/apache/beam/issues/24685
  beam.typehints.disable_type_annotations()

  with beam.Pipeline(runner=BEAM_RUNNER.value) as root:
    _ = (
        root
        | beam.Create([i * time_chunk_size for i in range(time_chunk_count)])
        | beam.Map(lambda start: slice(start, start + time_chunk_size))
        | beam.FlatMap(select_climatology, climatology, times)
        | xbeam.ChunksToZarr(
            OUTPUT_PATH.value, template=template, zarr_chunks=output_chunks
        )
    )


if __name__ == '__main__':
  app.run(main)
  flags.mark_flag_as_required(['input_path', 'output_path'])